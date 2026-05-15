# UBS Manager Bot

UBS Manager Telegram Bot for automated trading (V2.2, V3.0, V4.0).

## Description
This bot automates the "Infinite Purchase Method" (무한매수법) trading strategies using the Korea Investment & Securities (KIS) API. It features a Telegram interface for monitoring, manual ordering, and automated scheduling.

## Features
- **Automated Trading**: Supports V2.2, V3.0, and V4.0 strategies.
- **Telegram Interface**: Real-time status updates, manual orders, and report generation.
- **Docker Support**: Easy deployment on Synology NAS or cloud environments.
- **Persistence**: State management using localized JSON storage.

## Setup
1. Clone the repository.
2. Create a `.env` file with your credentials (see `config_manager.py`).
3. Run using Docker: `docker-compose up -d`.
