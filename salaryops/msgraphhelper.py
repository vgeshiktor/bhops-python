"""
    MsGraphHelper class to get access token using ms graph api
"""

import os
import webbrowser
from typing import List

import msal

MS_GRAPH_BASE_URL = "https://graph.microsoft.com/v1.0"
MS_GRAPH_ME_EP = f"{MS_GRAPH_BASE_URL}/me"
MS_GRAPH_ME_MSGS_EP = f"{MS_GRAPH_BASE_URL}/me/messages"
MS_GRAPH_ME_FOLDERS_EP = f"{MS_GRAPH_BASE_URL}/me/mailFolders/"
MS_GRAPH_ME_SEND_EMAIL_EP = f"{MS_GRAPH_BASE_URL}/me/sendMail"
AUTHORITY = "https://login.microsoftonline.com/consumers"


class MsGraphHelper:
    """
    MsGraphHelper class to wrap msal library
    """

    def __init__(self, app_id: str, secret: str, scopes: List[str]) -> None:
        self.app_id = app_id
        self.secret = secret
        self.scopes = scopes

    def get_access_token(self) -> str:
        """
        Get access token using msal library
        """
        client = msal.ConfidentialClientApplication(
            self.app_id, authority=AUTHORITY, client_credential=self.secret
        )

        # check if there is a refresh token stored
        refresh_token = None
        if os.path.exists("refresh_token.txt"):
            with open("refresh_token.txt", "r", encoding="utf-8") as f:
                refresh_token = f.read().strip()

        if refresh_token:
            # try to acquire a new access token using the refresh token
            response = client.acquire_token_by_refresh_token(
                refresh_token, scopes=self.scopes
            )
            if "access_token" in response:
                return str(response["access_token"])
        else:
            # no refresh token, proceed with the authorization code flow
            auth_request_url = client.get_authorization_request_url(self.scopes)
            webbrowser.open(auth_request_url)
            if authorization_code := input("Enter the authorization code: "):
                response = client.acquire_token_by_authorization_code(
                    authorization_code, scopes=self.scopes
                )

            else:
                raise ValueError("Authorization code is empty")

        if "access_token" in response:
            # store the refresh token securely
            if "refresh_token" in response:
                with open("refresh_token.txt", "w", encoding="utf-8") as f:
                    f.write(response["refresh_token"])
            return str(response["access_token"])

        raise ValueError(f"Failed to acquire access token: {str(response)}")
