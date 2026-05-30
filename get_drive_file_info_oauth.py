import os.path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
import sys

# If modifying these scopes, delete the file token.json.
SCOPES = ["https://www.googleapis.com/auth/drive.metadata.readonly"]

def get_file_info_oauth(file_id, credentials_file):
    """
    Retrieves information about a file on Google Drive using User OAuth.
    This will open a browser for the user to authorize.
    """
    creds = None
    # The file token.json stores the user's access and refresh tokens, and is
    # created automatically when the authorization flow completes for the first
    # time.
    if os.path.exists("token.json"):
        creds = Credentials.from_authorized_user_file("token.json", SCOPES)
    
    # If there are no (valid) credentials available, let the user log in.
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(
                credentials_file, SCOPES
            )
            creds = flow.run_local_server(port=0)
        
        # Save the credentials for the next run
        with open("token.json", "w") as token:
            token.write(creds.to_json())

    try:
        service = build("drive", "v3", credentials=creds)

        # Call the Drive v3 API
        file_metadata = service.files().get(fileId=file_id).execute()

        print(f"File ID: {file_metadata.get('id')}")
        print(f"File Name: {file_metadata.get('name')}")
        print(f"MIME Type: {file_metadata.get('mimeType')}")
        return file_metadata

    except HttpError as error:
        print(f"An error occurred: {error}")
        return None

if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("Usage: python get_drive_file_info_oauth.py <file_id> <path_to_oauth_client_secret.json>")
    else:
        file_id = sys.argv[1]
        credentials_file = sys.argv[2]
        get_file_info_oauth(file_id, credentials_file)
