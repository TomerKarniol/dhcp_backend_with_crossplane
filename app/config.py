from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    DHCP_API_TOKEN: str = ""  # empty = auth disabled
    HOST: str = "0.0.0.0"
    PORT: int = 8080
    LOG_LEVEL: str = "INFO"

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


settings = Settings()
