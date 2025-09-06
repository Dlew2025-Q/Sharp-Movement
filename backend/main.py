import firebase_admin
from firebase_admin import credentials, firestore
import requests
import time
import json
from datetime import datetime, timezone

# --- CONFIGURATION ---
ODDS_API_KEY = 'cc51a757d14174fd8061956b288df39e'
ODDS_API_BASE_URL = 'https://api.the-odds-api.com/v4/sports'
# NOTE: Add or remove sports as needed
SPORTS_LIST = ['americanfootball_nfl', 'americanfootball_ncaaf', 'basketball_nba', 'icehockey_nhl', 'baseball_mlb']

# --- Firebase Configuration ---
# IMPORTANT: Download your serviceAccountKey.json from your Firebase project settings
# and place it in the same directory as this script.
# Firebase -> Project Settings -> Service accounts -> Generate new private key
SERVICE_ACCOUNT_KEY_PATH = "serviceAccountKey.json"
# IMPORTANT: Replace with your Firebase App ID. Find it in your index.html file's __app_id variable or Firebase project settings.
APP_ID = "default-canvas-app-id" 
# This is the path where your data is stored.
COLLECTION_PATH = f'artifacts/{APP_ID}/public/data/sports_odds'

# --- Analysis Thresholds ---
MONEYLINE_MOVE_THRESHOLD = 0.20
SPREAD_POINT_MOVE_THRESHOLD = 1.0
TOTAL_POINT_MOVE_THRESHOLD = 1.0

# --- Helper Functions ---
def get_number_or_null(value):
    """Safely convert a value to a float, returning None if not possible."""
    try:
        return float(value)
    except (ValueError, TypeError):
        return None

def initialize_firebase():
    """Initializes the Firebase Admin SDK."""
    try:
        cred = credentials.Certificate(SERVICE_ACCOUNT_KEY_PATH)
        firebase_admin.initialize_app(cred)
        print("Firebase Admin SDK initialized successfully.")
        return firestore.client()
    except Exception as e:
        print(f"Error initializing Firebase: {e}")
        print("Please ensure your serviceAccountKey.json is in the correct path.")
        return None

# --- Core Functions ---
def fetch_and_save_odds(db):
    """Fetches odds for all sports in SPORTS_LIST and saves them to Firestore."""
    print("Starting odds fetch process...")
    end_of_today = datetime.now().replace(hour=23, minute=59, second=59, microsecond=999)

    for sport in SPORTS_LIST:
        print(f"  Fetching odds for: {sport}")
        try:
            url = f"{ODDS_API_BASE_URL}/{sport}/odds/?apiKey={ODDS_API_KEY}&regions=us&markets=h2h,spreads,totals&oddsFormat=decimal"
            response = requests.get(url)
            response.raise_for_status()  # Raises an exception for bad status codes
            data = response.json()

            batch = db.batch()
            for event in data:
                # Only process games happening today
                commence_time = datetime.fromisoformat(event['commence_time'].replace('Z', '+00:00'))
                if commence_time.date() > end_of_today.date():
                    continue

                bookmaker = next((b for b in event.get('bookmakers', []) if b['title'] == 'DraftKings'), None)
                if not bookmaker:
                    continue
                
                # Create a new document in the batch
                doc_ref = db.collection(COLLECTION_PATH).document()
                h2h = next((m['outcomes'] for m in bookmaker.get('markets', []) if m['key'] == 'h2h'), [])
                spreads = next((m['outcomes'] for m in bookmaker.get('markets', []) if m['key'] == 'spreads'), [])
                totals = next((m['outcomes'] for m in bookmaker.get('markets', []) if m['key'] == 'totals'), [])
                
                odds_doc = {
                    'Timestamp': datetime.now(timezone.utc).isoformat(),
                    'EventId': event['id'],
                    'Sport': event['sport_title'],
                    'Event': f"{event['home_team']} vs {event['away_team']}",
                    'CommenceTime': event['commence_time'],
                    'Team1': event['home_team'],
                    'Team2': event['away_team'],
                    'Bookmaker': bookmaker['title'],
                    'OddsTeam1': get_number_or_null(next((o['price'] for o in h2h if o['name'] == event['home_team']), None)),
                    'OddsTeam2': get_number_or_null(next((o['price'] for o in h2h if o['name'] == event['away_team']), None)),
                    'SpreadTeam1Point': get_number_or_null(next((o['point'] for o in spreads if o['name'] == event['home_team']), None)),
                    'SpreadTeam1Price': get_number_or_null(next((o['price'] for o in spreads if o['name'] == event['home_team']), None)),
                    'TotalPoint': get_number_or_null(next((o['point'] for o in totals if o['name'] == 'Over'), None)),
                    'TotalOverPrice': get_number_or_null(next((o['price'] for o in totals if o['name'] == 'Over'), None)),
                    'TotalUnderPrice': get_number_or_null(next((o['price'] for o in totals if o['name'] == 'Under'), None)),
                }
                batch.set(doc_ref, odds_doc)
            
            batch.commit()
            print(f"    Successfully saved {len(data)} events for {sport}.")

        except requests.exceptions.RequestException as e:
            print(f"    Error fetching data for {sport}: {e}")
        except Exception as e:
            print(f"    An unexpected error occurred for {sport}: {e}")
        
        time.sleep(2) # Brief pause to respect API rate limits between sports

def run_ai_analysis(db):
    """Analyzes games with significant line movement using the Gemini API."""
    print("\nStarting AI analysis process...")
    all_odds_docs = db.collection(COLLECTION_PATH).stream()
    
    odds_by_event = {}
    for doc in all_odds_docs:
        data = doc.to_dict()
        event_id = data.get('EventId')
        if event_id:
            if event_id not in odds_by_event:
                odds_by_event[event_id] = []
            # Add the firestore doc id for later updates
            data['id'] = doc.id 
            odds_by_event[event_id].append(data)

    print(f"Found {len(odds_by_event)} unique games in the database.")

    events_to_analyze = []
    for event_id, odds_list in odds_by_event.items():
        if len(odds_list) < 2:
            continue
            
        sorted_odds = sorted(odds_list, key=lambda x: x['Timestamp'])
        first = sorted_odds[0]
        last = sorted_odds[-1]

        # Skip games that have already started
        if datetime.fromisoformat(last['CommenceTime'].replace('Z', '+00:00')) < datetime.now(timezone.utc):
            continue

        # Check for significant movement
        h2h_move = abs((first.get('OddsTeam1') or 0) - (last.get('OddsTeam1') or 0)) >= MONEYLINE_MOVE_THRESHOLD
        spread_move = abs((first.get('SpreadTeam1Point') or 0) - (last.get('SpreadTeam1Point') or 0)) >= SPREAD_POINT_MOVE_THRESHOLD
        total_move = abs((first.get('TotalPoint') or 0) - (last.get('TotalPoint') or 0)) >= TOTAL_POINT_MOVE_THRESHOLD

        if h2h_move or spread_move or total_move:
            events_to_analyze.append(sorted_odds)

    if not events_to_analyze:
        print("No games with significant, unplayed line movement found.")
        return

    print(f"Found {len(eventsToAnalyze)} games that meet the analysis thresholds.")
    batch = db.batch()
    analysis_count = 0

    for event_odds in events_to_analyze:
        analysis_count += 1
        latest_odd = event_odds[-1]
        print(f"  Analyzing {analysis_count}/{len(eventsToAnalyze)}: {latest_odd['Event']}")

        history = [{
            'timestamp': o['Timestamp'],
            'team1_h2h': o.get('OddsTeam1'),
            'team2_h2h': o.get('OddsTeam2'),
            'team1_spread': o.get('SpreadTeam1Point'),
            'total_point': o.get('TotalPoint')
        } for o in event_odds]

        system_prompt = "You are a world-class sports betting analyst..."
        user_query = f"Game: {latest_odd['Event']} ({latest_odd['Sport']})\nOdds History (JSON):\n{json.dumps(history, indent=2)}\n\nBased ONLY on the line movement, what is the sharpest play?"
        
        # This is a placeholder for the Gemini API call.
        # In a real scenario, you would use the `google-cloud-aiplatform` library.
        # For simplicity, we'll simulate a response here.
        try:
            # SIMULATED API CALL - Replace with actual Gemini call
            # Example using a library like `google.generativeai`
            # model = genai.GenerativeModel('gemini-pro')
            # response = model.generate_content(user_query, system_instruction=system_prompt)
            # analysis_data = json.loads(response.text)
            
            # Since we can't make a real call, we'll just log it for now.
            # In a real implementation, the response would come from the AI.
            print(f"    (Simulated) AI call for {latest_odd['Event']}. In a real app, this would update the database.")
            # Example of how you would update the doc:
            # doc_ref = db.collection(COLLECTION_PATH).document(latest_odd['id'])
            # batch.update(doc_ref, {
            #     'Recommendation': analysis_data['reasoning'],
            #     'AIOutcome': analysis_data['outcome'],
            #     'Confidence': analysis_data['confidence']
            # })

        except Exception as e:
            print(f"    AI analysis failed for {latest_odd['Event']}: {e}")

    # In a real implementation, you would commit the batch here
    # await batch.commit()
    print("AI analysis simulation complete.")


if __name__ == "__main__":
    db_client = initialize_firebase()
    if db_client:
        # This script can be run on a schedule (e.g., using cron on a server)
        # For this example, we will just run it once.
        fetch_and_save_odds(db_client)
        run_ai_analysis(db_client)
        print("\nScript finished.")
