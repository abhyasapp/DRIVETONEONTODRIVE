import pickle
import json

with open('oauth-token.pickle', 'rb') as f:
    creds = pickle.load(f)

token_json = json.dumps({
    'token': creds.token,
    'refresh_token': creds.refresh_token,
    'token_uri': creds.token_uri,
    'client_id': creds.client_id,
    'client_secret': creds.client_secret,
    'scopes': creds.scopes
})
print(token_json)