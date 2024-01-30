#!/usr/bin/env python3
import logging
import sys
from systemd import journal


class Logger:
    def __init__(self, nolog=False):
        self.interactive = sys.stdin.isatty()
        self.nolog = nolog

        # Create a logger
        self.logger = logging.getLogger(__name__)

        # Configure logging to send messages to the console (stdout)
        self.console_handler = logging.StreamHandler()
        self.console_handler.setLevel(logging.INFO)  # Set the desired log level
        formatter = logging.Formatter("%(levelname)s - %(message)s")
        self.console_handler.setFormatter(formatter)
        self.logger.addHandler(self.console_handler)

    def __send_errors(self, msg):
        logging.error(msg)
        journal.send(msg, PRIORITY="ERROR")

    def __send_logs(self, msg):
        logging.info(msg)
        journal.send(msg, PRIORITY="INFO")

    def log(self, msg):
        if self.interactive:
            print(msg)
        if not self.nolog:
            self.__send_logs(msg)

    def log_error(self, msg):
        if self.interactive:
            print(msg)
        if not self.nolog:
            self.__send_errors(msg)
