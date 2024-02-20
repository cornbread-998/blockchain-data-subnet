import os
import json
import time

import traceback
from typing import Callable, Awaitable, List, Optional

import bittensor as bt
from bittensor.axon import FastAPIThreadedServer
from fastapi import  APIRouter
from fastapi.responses import JSONResponse
from fastapi import FastAPI, Request
from pyngrok import ngrok
import uvicorn

from insights.protocol import Query, get_networks, get_default_model_by_network, get_supported_model_by_network

ForwardFn = Callable[[Query], Awaitable[Query]]

auth_data = dict()
request_counts = {}

def is_api_data_valid(data):
    if not isinstance(data, dict):
        return False, "Not a dictionary"

    if "keys" not in data.keys():
        return False, "Missing users key"

    if not isinstance(data["keys"], dict):
        return False, "Keys field is not a dict"

    for key, value in data["keys"].items():
        if not isinstance(value, dict):
            return False, "Key value is not a dictionary"
        if "requests_per_min" not in value.keys():
            return False, "Missing requests_per_min field"
        if not isinstance(value["requests_per_min"], int):
            return False, "requests_per_min is not an int"

    return True, "Formatting is good"


def load_api_config():
    bt.logging.debug("Loading API config")

    try:
        if not os.path.exists("neurons/validators/api.json"):
            raise Exception(f"{'neurons/validators/api.json'} does not exist")

        with open("neurons/validators/api.json", 'r') as file:
            api_data = json.load(file)
            bt.logging.trace("api_data", api_data)

            valid, reason = is_api_data_valid(api_data)
            if not valid:
                raise Exception(f"{'neurons/validators/api.json'} is poorly formatted. {reason}")
            if "change-me" in api_data["keys"]:
                bt.logging.warning("YOU ARE USING THE DEFAULT API KEY. CHANGE IT FOR SECURITY REASONS.")
        return api_data
    
    except Exception as e:
        bt.logging.error("Error loading API config:", e)
        traceback.print_exc()


async def auth_rate_limiting_middleware(request: Request, call_next):
    auth_api = request.headers.get('auth')
    auth_data = load_api_config()
    time_window = 60

    bt.logging.info("auth_data", auth_data)

    if auth_api not in  auth_data["keys"].keys():
        bt.logging.debug(f"Unauthorized key: {auth_api}")
        return JSONResponse(status_code=401, content={"detail": "Unauthorized",
                                                      "output": []})

    requests_per_min = auth_data["keys"][auth_api]["requests_per_min"]

    # Rate limiting
    current_time = time.time()
    if auth_api in request_counts:
        requests, start_time = request_counts[auth_api]

        if current_time - start_time > time_window:
            # start a new time period
            request_counts[auth_api] = (1, current_time)
        elif requests < requests_per_min:
            # same time period
            request_counts[auth_api] = (requests + 1, start_time)
        else:
            bt.logging.debug(f"Rate limit exceeded for key: {auth_api}")
            return JSONResponse(status_code=429, content={"detail": "Rate limit exceeded", "output": []})
    else:
        request_counts[auth_api] = (1, current_time)

    response = await call_next(request)
    return response

def connect_ngrok_tunnel(local_port: int, domain: str) -> ngrok.NgrokTunnel:
    auth_token = os.environ.get('NGROK_AUTH_TOKEN', None)
    if auth_token is not None:
        ngrok.set_auth_token(auth_token)

    tunnel = ngrok.connect(
        addr=str(local_port),
        proto="http",
        # Domain is required.
        domain=domain
    )
    bt.logging.info(
        f"API is available over NGROK at {tunnel.public_url}"
    )

    return tunnel

class ApiServer:
    app: FastAPI
    fast_server: FastAPIThreadedServer
    router: APIRouter
    forward_fn: ForwardFn
    tunnel: Optional[ngrok.NgrokTunnel]
    ngrok_domain: Optional[str]

    def __init__(
            self, 
            axon_port: int,
            forward_fn: ForwardFn,
            api_json: str,
            ngrok_domain: Optional[str]
    ):

        self.forward_fn = forward_fn
        self.app = FastAPI()
        self.app.middleware('http')(auth_rate_limiting_middleware)

        self.fast_server = FastAPIThreadedServer(config=uvicorn.Config(
            self.app,
            host="0.0.0.0",
            port=axon_port,
            log_level="trace" if bt.logging.__trace_on__ else "critical"
        ))
        self.router = APIRouter()
        self.router.add_api_route(
            "/query",
            self.query,
            methods=["POST"],
        )
        self.app.include_router(self.router)

        self.api_json = api_json

        self.ngrok_domain = ngrok_domain
        self.tunnel = None
        self.supported_network = get_networks()


    async def query(self, request: Query):
        if request.network not in self.supported_network:
            return JSONResponse(
                status_code=400,
                content={
                    "detail": f"Invalid network. please use a supported network: {self.supported_network}",
                })
        
        supported_models_for_network = get_supported_model_by_network(request.network)
        if request.model_type is None:
            model_type = get_default_model_by_network(request.network)
        elif request.model_type not in supported_models_for_network:
            return JSONResponse(
                status_code=400,
                content={
                    "detail": f"Invalid model_type. please us a supported model for network {request.network}: {supported_models_for_network}",
                })  
        else:
            model_type = request.model_type

        if request.query is None or request.query == '':
            return JSONResponse(
                status_code=400,
                content={
                    "detail": f"Invalid Query. Query is empty",
                })   

        # Recreate the synapse with the source_lang.
        request = Query(
            network=request.network,
            model_type=model_type,
            query=request.query,
        )


        response = await self.forward_fn(request)
        bt.logging.debug(f"API: response.output {response.output}")
        return JSONResponse(status_code=200,
                            content={"detail": "success", "output": response.output})

    def start(self):
        self.fast_server.start()

        if self.ngrok_domain is not None:
            self.tunnel = connect_ngrok_tunnel(
                local_port=self.fast_server.config.port,
                domain=self.ngrok_domain
            )

    def stop(self):
        self.fast_server.stop()

        if self.tunnel is not None:
            ngrok.disconnect(
                public_url=self.tunnel.public_url
            )
            self.tunnel = None


