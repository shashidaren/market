import logging
from app.core.config import settings


def setup_logging():
    log_file = settings.LOG_DIR / "i_report.log"

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        handlers=[
            logging.FileHandler(log_file),
            logging.StreamHandler(),
        ],
    )
