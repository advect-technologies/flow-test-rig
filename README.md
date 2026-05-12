## Instructions
1. Open up Windows Terminal
1. Install the Python environment manager [uv](https://docs.astral.sh/uv/getting-started/installation/)
1. Copy ```default_config.toml``` into the project root directory as ```config.toml``` and adjust as needed
1. Copy ```default_daq_config.toml``` into the project root directory as ```daq_config.toml``` and adjust as needed
1. From the root project directory, run, ```uv run flow_app.py```

* Scale - [A&D EK-30KL](https://www.aandd.jp/products/manual/balances/ek-l_manual.pdf)
    * Default serial settings are
        - baud rate = 2400
        - data bits = 7
        - parity = even parity
        - carriage return + line feed
    * These settings can be reconfigured. See section 8 in the linked user manual