"""Logging configuration for Monad Validator Monitor"""

import io
import logging
import sys
from datetime import datetime
from typing import Optional


class SafeStreamHandler(logging.StreamHandler):
    """StreamHandler that safely handles Unicode on Windows"""

    def emit(self, record):
        try:
            msg = self.format(record)
            stream = self.stream
            # Ensure UTF-8 encoding for output
            if hasattr(stream, 'buffer'):
                stream.buffer.write((msg + self.terminator).encode('utf-8', errors='replace'))
                stream.buffer.flush()
            else:
                stream.write(msg + self.terminator)
                stream.flush()
        except Exception:
            self.handleError(record)


class ColoredFormatter(logging.Formatter):
    """Custom formatter with colors for different log levels"""

    # ANSI color codes
    COLORS = {
        'DEBUG': '\033[36m',    # Cyan
        'INFO': '\033[32m',     # Green
        'WARNING': '\033[33m',  # Yellow
        'ERROR': '\033[31m',    # Red
        'CRITICAL': '\033[35m', # Magenta
    }
    RESET = '\033[0m'

    def format(self, record):
        # Get base formatted message
        formatted = super().format(record)

        # Add color based on level
        color = self.COLORS.get(record.levelname, '')
        if color:
            formatted = f"{color}{formatted}{self.RESET}"

        return formatted


def setup_logger(
    name: str = "monad_monitor",
    level: str = "INFO",
    log_file: Optional[str] = None
) -> logging.Logger:
    """
    Setup logger with console and optional file handler.

    Args:
        name: Logger name
        level: Log level (DEBUG, INFO, WARNING, ERROR, CRITICAL)
        log_file: Optional file path for logging

    Returns:
        Configured logger instance
    """
    logger = logging.getLogger(name)
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))

    # Clear existing handlers
    logger.handlers.clear()

    # Console handler with safe Unicode handling
    console_handler = SafeStreamHandler(sys.stdout)
    console_handler.setLevel(logging.DEBUG)

    # Format: [HH:MM:SS] LEVEL Message
    formatter = ColoredFormatter(
        fmt='[%(asctime)s] %(levelname)s %(message)s',
        datefmt='%H:%M:%S'
    )
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    # Optional file handler
    if log_file:
        file_handler = logging.FileHandler(log_file, encoding='utf-8')
        file_handler.setLevel(logging.DEBUG)
        file_formatter = logging.Formatter(
            fmt='[%(asctime)s] %(levelname)s %(name)s: %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        )
        file_handler.setFormatter(file_formatter)
        logger.addHandler(file_handler)

    return logger


# Global logger instance
_logger: Optional[logging.Logger] = None


def get_logger() -> logging.Logger:
    """Get or create the global logger instance"""
    global _logger
    if _logger is None:
        _logger = setup_logger()
    return _logger


def init_logger(level: str = "INFO", log_file: Optional[str] = None) -> logging.Logger:
    """Initialize the global logger with custom settings"""
    global _logger
    _logger = setup_logger(level=level, log_file=log_file)
    return _logger


# Convenience functions
def debug(msg: str):
    get_logger().debug(msg)


def info(msg: str):
    get_logger().info(msg)


def warning(msg: str):
    get_logger().warning(msg)


def error(msg: str):
    get_logger().error(msg)


def critical(msg: str):
    get_logger().critical(msg)
