from datetime import datetime
import schedule
import time
import logging

class Scheduler:
    """Handles scheduling tasks for checking product prices at regular intervals."""

    def __init__(self, interval_minutes: int, check_function):
        self.interval_minutes = interval_minutes
        self.check_function = check_function

    def start(self):
        """Start the scheduler to check prices at the specified interval."""
        schedule.every(self.interval_minutes).minutes.do(self.check_function)
        logging.info(f"Scheduler started: checking prices every {self.interval_minutes} minutes.")
        
        try:
            while True:
                schedule.run_pending()
                time.sleep(1)
        except KeyboardInterrupt:
            logging.info("Scheduler stopped by user.")

    def run_once(self):
        """Run the check function once immediately."""
        logging.info("Running price check immediately.")
        self.check_function()