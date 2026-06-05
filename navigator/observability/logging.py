import structlog
def get_logger(name: str = "navigator"):
    return structlog.get_logger(name)
