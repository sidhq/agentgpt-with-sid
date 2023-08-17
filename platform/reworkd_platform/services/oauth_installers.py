# from slack.web import WebClient
from abc import ABC, abstractmethod
from urllib.parse import urlencode
from datetime import datetime, timedelta

import json
import aiohttp
from fastapi import Depends, Path
from slack_sdk import WebClient
from slack_sdk.oauth import AuthorizeUrlGenerator

from reworkd_platform.db.crud.oauth import OAuthCrud
from reworkd_platform.db.models.auth import OauthCredentials
from reworkd_platform.schemas import UserBase
from reworkd_platform.services.security import encryption_service
from reworkd_platform.settings import Settings, settings as platform_settings
from reworkd_platform.web.api.http_responses import forbidden


class OAuthInstaller(ABC):
    def __init__(self, crud: OAuthCrud, settings: Settings):
        self.crud = crud
        self.settings = settings

    @abstractmethod
    async def install(self, user: UserBase, redirect_uri: str) -> str:
        raise NotImplementedError()

    @abstractmethod
    async def install_callback(self, code: str, state: str) -> OauthCredentials:
        raise NotImplementedError()

    @abstractmethod
    async def uninstall(self, user: UserBase) -> bool:
        raise NotImplementedError()

    @staticmethod
    def store_access_token(creds: OauthCredentials, access_token: str) -> None:
        creds.access_token_enc = encryption_service.encrypt(access_token)

    @staticmethod
    def store_refresh_token(creds: OauthCredentials, refresh_token: str) -> None:
        creds.refresh_token_enc = encryption_service.encrypt(refresh_token)


class SlackInstaller(OAuthInstaller):
    PROVIDER = "slack"

    async def install(self, user: UserBase, redirect_uri: str) -> str:
        installation = await self.crud.create_installation(
            user, self.PROVIDER, redirect_uri
        )

        return AuthorizeUrlGenerator(
            client_id=self.settings.slack_client_id,
            redirect_uri=self.settings.slack_redirect_uri,
            scopes=["chat:write"],
        ).generate(
            state=installation.state,
        )

    async def install_callback(self, code: str, state: str) -> OauthCredentials:
        creds = await self.crud.get_installation_by_state(state)
        if not creds:
            raise forbidden()

        oauth_response = WebClient().oauth_v2_access(
            client_id=self.settings.slack_client_id,
            client_secret=self.settings.slack_client_secret,
            code=code,
            state=state,
            redirect_uri=self.settings.slack_redirect_uri,
        )

        OAuthInstaller.store_access_token(creds, oauth_response["access_token"])
        creds.token_type = oauth_response["token_type"]
        creds.scope = oauth_response["scope"]
        return await creds.save(self.crud.session)

    async def uninstall(self, user: UserBase) -> bool:
        raise NotImplementedError()

class SIDInstaller(OAuthInstaller):
    PROVIDER = "sid"

    async def install(self, user: UserBase, redirect_uri: str) -> str:
        installation = await self.crud.get_installation_by_user_id(user.id, self.PROVIDER)
        if installation:
            installation.delete_date = None # un-delete
        else:
            installation = await self.crud.create_installation(
                user, self.PROVIDER, redirect_uri,
            )
        scopes = ["data:query", "offline_access"]
        params = {
            'client_id': self.settings.sid_client_id,
            'redirect_uri': self.settings.sid_redirect_uri,
            'response_type': 'code',
            'scope': ' '.join(scopes),
            'state': installation.state,
            'audience': 'https://api.sid.ai/api/v1/',
            'app_name': 'AgentGPT'
        }
        auth_url = 'https://me.sid.ai/api/oauth/authorize'
        auth_url += '?' + urlencode(params)
        return auth_url

    async def install_callback(self, code: str, state: str) -> OauthCredentials:
        creds = await self.crud.get_installation_by_state(state)
        if not creds:
            raise forbidden()
        req = {
            'grant_type': 'authorization_code',
            'client_id': self.settings.sid_client_id,
            'client_secret': self.settings.sid_client_secret,
            'redirect_uri': self.settings.sid_redirect_uri,
            'code': code,
        }
        async with aiohttp.ClientSession() as session:
            async with session.post(
                "https://auth.sid.ai/oauth/token",
                headers={
                    'Content-Type': 'application/json',
                    'Accept': 'application/json',
                },
                data=json.dumps(req)
            ) as response:
                res_data = await response.json()

        OAuthInstaller.store_access_token(creds, res_data["access_token"])
        OAuthInstaller.store_refresh_token(creds, res_data["refresh_token"])
        creds.access_token_expiration = datetime.now() + timedelta(seconds=res_data["expires_in"])
        return await creds.save(self.crud.session)

    async def uninstall(self, user: UserBase) -> bool:
        creds = await self.crud.get_installation_by_user_id(user.id, self.PROVIDER)
        # check if credentials exist and contain a refresh token
        if not creds or creds.access_token_enc == "":
            return False
        
        # use refresh token to revoke access
        delete_token = encryption_service.decrypt(creds.refresh_token_enc)

        # delete credentials from database
        creds.access_token_enc = ""
        creds.refresh_token_enc = ""
        await creds.delete(self.crud.session)

        # revoke refresh token
        async with aiohttp.ClientSession() as session:
            await session.post(
                "https://auth.sid.ai/oauth/revoke",
                headers={
                    'Content-Type': 'application/json',
                },
                data=json.dumps({
                    'client_id': self.settings.sid_client_id,
                    'client_secret': self.settings.sid_client_secret,
                    'token': delete_token,
                })
            )
        return True


integrations = {
    SlackInstaller.PROVIDER: SlackInstaller,
    SIDInstaller.PROVIDER: SIDInstaller,
}


def installer_factory(
    provider: str = Path(description="OAuth Provider"),
    crud: OAuthCrud = Depends(OAuthCrud.inject),
) -> OAuthInstaller:
    """Factory for OAuth installers
    Args:
        provider (str): OAuth Provider (can be slack, github, etc.) (injected)
        crud (OAuthCrud): OAuth Crud (injected)
    """

    if provider in integrations:
        return integrations[provider](crud, platform_settings)
    raise NotImplementedError()
