from instagrapi import Client
from typing import List, Dict


class InstagramClient:
    def __init__(self):
        self.client = Client()
        self.authenticated = False

    def authenticate(self, username: str, password: str) -> bool:
        try:
            self.client.login(username, password)
            self.authenticated = True
            return True
        except Exception as e:
            self.authenticated = False
            raise Exception(f"Authentication failed: {str(e)}")

    def send_dm(self, user_ids: List[int], message: str) -> bool:
        if not self.authenticated:
            raise Exception("Client not authenticated")

        try:
            self.client.direct_send(message, user_ids)
            return True
        except Exception as e:
            raise Exception(f"Failed to send DM: {str(e)}")

    def get_followers(self, user_id: int = None) -> List[Dict]:
        if not self.authenticated:
            raise Exception("Client not authenticated")

        try:
            if user_id is None:
                user_id = self.client.user_id

            followers = self.client.user_followers(user_id)
            return [
                {
                    "user_id": user.pk,
                    "username": user.username,
                    "full_name": user.full_name
                }
                for user in followers.values()
            ]
        except Exception as e:
            raise Exception(f"Failed to get followers: {str(e)}")

    def get_recent_comments(self, media_id: str = None, count: int = 20) -> List[Dict]:
        if not self.authenticated:
            raise Exception("Client not authenticated")

        try:
            if media_id is None:
                user_medias = self.client.user_medias(self.client.user_id, amount=1)
                if not user_medias:
                    return []
                media_id = user_medias[0].pk

            comments = self.client.media_comments(media_id, amount=count)
            return [
                {
                    "comment_id": comment.pk,
                    "user_id": comment.user.pk,
                    "username": comment.user.username,
                    "text": comment.text,
                    "created_at": comment.created_at_utc
                }
                for comment in comments
            ]
        except Exception as e:
            raise Exception(f"Failed to get recent comments: {str(e)}")
