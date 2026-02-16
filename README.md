# Google Drive File Info Script

This script retrieves information about a file on Google Drive using a Google Cloud Service Account.

## Prerequisites

1.  Python 3.x
2.  A Google Cloud Service Account with a `credential.json` key file.
3.  The Service Account email must be shared with the Google Drive file you want to access.
4.  Install the required libraries:

    ```bash
    pip install -r requirements.txt
    ```

## Usage

```bash
python get_drive_file_info.py <file_id> <path_to_credential.json>
```

Example:

```bash
python get_drive_file_info.py 1234567890abcdefg path/to/credential.json
```
