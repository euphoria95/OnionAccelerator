# OnionAccelerator
OnionAccelerator is a project designed to exponentially increase the download speed of files by leveraging multiple TOR nodes running in Docker containers. This innovative approach allows for parallel downloading through multiple proxy servers, significantly boosting the overall download performance.
 
## Introduction
 
OnionAccelerator harnesses the power of Docker and TOR proxies to split file downloads into multiple parts, each fetched through a different proxy. This method not only speeds up the download process but also provides a layer of anonymity and security by routing traffic through the TOR network.
 
The project supports two modes of operation:
1. **Partial Mode**: Splits a single file into multiple parts and downloads each part through a different proxy.
2. **Concurrent Mode**: Downloads multiple files simultaneously, each through a different proxy.
 
## Features
 
- **Exponential Speed Increase**: By using multiple TOR nodes, download speeds are significantly increased.
- **Anonymity and Security**: All traffic is routed through the TOR network, providing enhanced privacy.
- **Docker Integration**: Easy deployment and management of TOR proxies using Docker.
 
## Getting Started
 
### Prerequisites
 
- Docker
- Bash
- curl
 
### Configuration
 
Create a `config.cfg` file with the following content:
 
```cfg
# Proxy configuration
PROXY_IP=127.0.0.1
PROXY_PORTS=5000
PROXY_COUNT=21
PROXY_TYPE=SOCKS5
User_Agent=YourUserAgentStringHere
'''

### Usage
Create a file named URLs.txt containing the URLs of the files you want to download.
Run the script with the desired mode:
'''
./OnionAccelerator.sh /path/to/download partial
 or
./OnionAccelerator.sh /path/to/download concurrent
'''

