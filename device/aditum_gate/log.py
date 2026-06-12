import logging


def setup_logging(level=logging.INFO):
    """Logging a stdout: PM2 lo captura y rota con pm2-logrotate."""
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    # Las librerias HTTP son muy ruidosas en INFO
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("werkzeug").setLevel(logging.WARNING)
