import google.auth
from google.oauth2 import service_account
from googleapiclient.discovery import build
import sys

def get_file_info(file_id, credentials_file):
    """
    Retrieves information about a file on Google Drive using a service account.

    Args:
        file_id (str): The ID of the file to retrieve.
        credentials_file (str): The path to the service account credential JSON file.
    """
    SCOPES = ['https://www.googleapis.com/auth/drive.metadata.readonly']

    try:
        creds = service_account.Credentials.from_service_account_file(
            credentials_file, scopes=SCOPES)

        service = build('drive', 'v3', credentials=creds)

        # Call the Drive v3 API
        file_metadata = service.files().get(fileId=file_id).execute()

        print(f"File ID: {file_metadata.get('id')}")
        print(f"File Name: {file_metadata.get('name')}")
        print(f"MIME Type: {file_metadata.get('mimeType')}")
        return file_metadata

    except Exception as e:
        print(f"An error occurred: {e}")
        return None

if __name__ == '__main__':
    if len(sys.argv) != 3:
        print("Usage: python get_drive_file_info.py <file_id> <path_to_credential.json>")
    else:
        file_id = sys.argv[1]
        credentials_file = sys.argv[2]
        get_file_info(file_id, credentials_file)
