# Sale Monitor Next

## Overview
Sale Monitor Next is an enhanced version of the Sale Monitor application, designed to monitor product prices and send notifications when they go on sale. This project improves local file handling and lays the groundwork for future web-based functionality.

## Features
- Monitor product prices from various online retailers.
- Send email notifications when prices drop below a specified target.
- Support for multiple storage backends (JSON and SQLite).
- Scheduled price checks to ensure timely notifications.
- Future web-based interface for easier management and monitoring.

## Project Structure
```
sale-monitor-next
├── src
│   └── sale_monitor
│       ├── __init__.py
│       ├── domain
│       │   ├── __init__.py
│       │   └── models.py
│       ├── services
│       │   ├── __init__.py
│       │   ├── price_extractor.py
│       │   ├── notifications.py
│       │   └── scheduler.py
│       ├── storage
│       │   ├── __init__.py
│       │   ├── base.py
│       │   ├── json_store.py
│       │   ├── sqlite_store.py
│       │   └── file_lock.py
│       ├── cli
│       │   ├── __init__.py
│       │   └── main.py
│       └── web
│           ├── __init__.py
│           ├── app.py
│           └── routes
│               ├── __init__.py
│               └── products.py
├── tests
│   ├── __init__.py
│   ├── test_storage_json.py
│   └── test_price_extractor.py
├── config
│   ├── config.example.json
│   └── logging.example.ini
├── data
│   └── .gitkeep
├── .env.example
├── .gitignore
├── pyproject.toml
├── requirements.txt
└── README.md
```

## Installation
1. Clone the repository:
   ```
   git clone <repository-url>
   cd sale-monitor-next
   ```

2. Install the required dependencies:
   ```
   pip install -r requirements.txt
   ```

3. Set up your environment variables by copying `.env.example` to `.env` and filling in the necessary values.

4. Configure the application by copying `config/config.example.json` to `config/config.json` and adjusting the settings as needed.

## Usage
To run the application, use the command line interface:
```
python -m src.sale_monitor.cli.main
```

You can also run the application with scheduling or debug specific features using the available command-line arguments.

## Contribution
Contributions are welcome! Please submit a pull request or open an issue for any enhancements or bug fixes.

## License
This project is licensed under the MIT License. See the LICENSE file for more details.