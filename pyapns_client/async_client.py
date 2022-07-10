from typing import Union
import httpx
import jwt
import json
import time

from . import exceptions
from .logging import logger


class AsyncAPNSClient:

    MODE_PROD = "prod"
    MODE_DEV = "dev"

    BASE_URLS = {
        MODE_PROD: "https://api.push.apple.com:443",
        MODE_DEV: "https://api.development.push.apple.com:443",
    }

    AUTH_TOKEN_LIFETIME = 45 * 60  # seconds
    AUTH_TOKEN_ENCRYPTION = "ES256"

    def __init__(
        self,
        mode: str,
        *,
        root_cert_path: Union[None, str, bool] = None,
        auth_key_path: Union[None, str] = None,
        auth_key_id: Union[None, str] = None,
        team_id: Union[None, str] = None,
        client_cert_path: Union[None, str] = None,
        client_cert_passphrase: Union[None, str] = None,
    ):
        """
        Initialize the APNSClient instance. Clients supports two types of authentication:
        - JWT authentication (auth_key_path, auth_key_id, team_id)
        - certificate authentication (client_cert_path, client_cert_passphrase)

        :param mode: The mode of the client. Either 'prod' or 'dev'.

        :param root_cert_path: The path to the root certificate.
        :param auth_key_path: The path to the authentication key.
        :param auth_key_id: The ID of the authentication key.
        :param team_id: The ID of the team.

        :param client_cert_path: The path to the client certificate.
        :param client_cert_passphrase: The passphrase of the client certificate.
        """
        super().__init__()

        if root_cert_path is None:
            root_cert_path = True

        self._base_url = self.BASE_URLS[mode]
        self._root_cert_path = root_cert_path
        self._auth_key = self._get_auth_key(auth_key_path) if auth_key_path else None
        self._auth_key_id = auth_key_id
        self._team_id = team_id

        self._client_cert_path = client_cert_path
        self._client_cert_passphrase = client_cert_passphrase

        if self._auth_key and self._auth_key_id and self._team_id:
            self._auth_type = "jwt"
        elif self._client_cert_path and self._client_cert_passphrase:
            self._auth_type = "cert"
        else:
            raise ValueError("Either the auth key or the client cert must be provided.")

        self._auth_token_time = None
        self._auth_token_storage = None
        self._client_storage = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()

    async def push(self, notification, device_token):
        headers = notification.get_headers()
        json_data = notification.get_json_data()

        logger.debug(
            f'Sending notification: {len(json_data)} bytes {json_data} to: "{device_token}".'
        )

        exc = None
        start_time = time.perf_counter()
        for _ in range(3):
            try:
                await self._push(
                    headers=headers, json_data=json_data, device_token=device_token
                )
                exc = None
                break
            except exceptions.APNSServerException as e:
                exc = e
                await self._reset_client()
            except exceptions.APNSException as e:
                exc = e
                break
        duration = round((time.perf_counter() - start_time) * 1000)

        if exc is not None:
            logger.debug(
                f"Failed to send the notification: {type(exc).__name__} {duration}ms."
            )
            raise exc

        logger.debug(f"Sent: {duration}ms.")

    async def close(self):
        await self._reset_client()
        self._reset_auth_token()
        logger.debug("Closed.")

    async def _push(self, headers, json_data, device_token):
        try:
            response = await self._send_request(
                headers=headers, json_data=json_data, device_token=device_token
            )
        except httpx.RequestError as e:
            logger.debug(f"Failed to receive a response: {type(e).__name__}.")
            raise exceptions.APNSConnectionException()

        status = "success" if response.status_code == 200 else "failure"
        logger.debug(f"Response received: {response.status_code} ({status}).")

        if response.status_code != 200:
            apns_id = response.headers.get("apns-id")
            apns_data = json.loads(response.text)
            reason = apns_data["reason"]

            logger.debug(f"Response reason: {reason}.")

            exception_class = self._get_exception_class(reason)
            exception_kwargs = {"status_code": response.status_code, "apns_id": apns_id}
            if issubclass(exception_class, exceptions.UnregisteredException):
                exception_kwargs["timestamp"] = apns_data["timestamp"]

            raise exception_class(**exception_kwargs)

    def _send_request(self, headers, json_data, device_token):
        url = f"/3/device/{device_token}"
        return self._client.post(url, data=json_data, headers=headers)

    def _authenticate_request(self, request):
        request.headers["authorization"] = f"bearer {self._auth_token}"
        return request

    @property
    def _auth_token(self):
        if self._auth_token_storage is None or self._is_auth_token_expired:
            logger.debug("Creating a new authentication token.")
            self._auth_token_time = time.time()
            token_dict = {"iss": self._team_id, "iat": self._auth_token_time}
            headers = {"alg": self.AUTH_TOKEN_ENCRYPTION, "kid": self._auth_key_id}
            auth_token = jwt.encode(
                token_dict,
                str(self._auth_key),
                algorithm=self.AUTH_TOKEN_ENCRYPTION,
                headers=headers,
            )
            self._auth_token_storage = auth_token

        return self._auth_token_storage

    @property
    def _client(self):
        if self._client_storage is None:
            logger.debug("Creating a new client instance.")
            limits = httpx.Limits(max_connections=1, max_keepalive_connections=0)
            self._client_storage = httpx.AsyncClient(
                auth=self._authenticate_request if self._auth_type == "jwt" else None,
                cert=(
                    str(self._client_cert_path),
                    self._client_cert_path,
                    self._client_cert_passphrase,
                )
                if self._auth_type == "cert"
                else None,
                verify=self._root_cert_path,
                http2=True,
                timeout=10.0,
                limits=limits,
                base_url=self._base_url,
            )

        return self._client_storage

    @property
    def _is_auth_token_expired(self):
        if self._auth_token_time is None:
            return True
        return time.time() >= self._auth_token_time + self.AUTH_TOKEN_LIFETIME

    def _reset_auth_token(self):
        logger.debug("Resetting the existing authentication token.")
        self._auth_token_time = None
        self._auth_token_storage = None

    async def _reset_client(self):
        logger.debug("Resetting the existing client instance.")
        if self._client_storage is not None:
            await self._client_storage.aclose()
        self._client_storage = None

    @staticmethod
    def _get_auth_key(auth_key_path):
        with open(auth_key_path) as f:
            return f.read()

    @staticmethod
    def _get_exception_class(reason):
        exception_class_name = f"{reason}Exception"
        try:
            return getattr(exceptions, exception_class_name)
        except AttributeError:
            raise NotImplementedError(f"Reason not implemented: {reason}")