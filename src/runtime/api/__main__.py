"""API entry point: `python -m runtime.api`."""

import uvicorn

from runtime.api.app import create_app
from runtime.config import ApiConfig
from runtime.logs import configure_logging


def main() -> None:
    configure_logging()
    config = ApiConfig.from_env()
    # log_config=None: uvicorn's loggers use the JSON handler set up above.
    uvicorn.run(create_app(config), host=config.host, port=config.port, log_config=None)


if __name__ == "__main__":
    main()
