#!/bin/bash

# Check the number of arguments
if [ "$#" -ne 2 ]; then
    echo "Usage: $0 <path to download files> <partial|concurrent>"
    exit 1
fi

# Read arguments
DOWNLOAD_PATH=$1
MODE=$2

# Load configuration
source config.cfg

# Logging function
log() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') - $1" | tee -a "$(date '+%Y-%m-%d_%H-%M-%S').log"
}

# Function to download a file using proxy
download_with_proxy() {
    local url=$1
    local proxy=$2
    local output=$3
    local range=$4

    log "curl --socks5-hostname \"$proxy\" --user-agent \"$User_Agent\" --retry 0 --retry-delay 1 --connect-timeout 5 -o \"$output\" \"$url\""
    if [ -z "$range" ]; then
        curl --socks5-hostname "$proxy" --user-agent "$User_Agent" --retry 0 --retry-delay 1 --connect-timeout 5 -o "$output" "$url"
    else
        curl --socks5-hostname "$proxy" --user-agent "$User_Agent" --retry 0 --retry-delay 1 --connect-timeout 5 -r "$range" -o "$output" "$url"
    fi
}

# Function to get the file size using proxy
get_file_size_with_proxy() {
    local url=$1
    local proxy=$2

    curl --socks5-hostname "$proxy" --user-agent "$User_Agent" -sI "$url" | grep -i Content-Length | awk '{print $2}' | tr -d '\r'
}

# Partial mode
partial_mode() {
    while IFS= read -r url; do
        proxy="127.0.0.1:5000"
        log "Fetching file size from URL: $url using proxy: $proxy"
        file_size=$(get_file_size_with_proxy "$url" "$proxy")
        if [ -z "$file_size" ]; then
            log "Failed to get file size for URL: $url"
            continue
        fi

        part_size=$((file_size / PROXY_COUNT))
        log "File size: $file_size bytes, part size: $part_size bytes"

        # Alphabet letters
        alphabet=( {A..Z} )

        for ((i=0; i<PROXY_COUNT; i++)); do
            start=$((i * part_size))
            end=$((start + part_size - 1))
            if [ $i -eq $((PROXY_COUNT - 1)) ]; then
                end=""
            fi

            range="$start-$end"
            proxy="127.0.0.1:$((5000 + i))"
            output="${DOWNLOAD_PATH}/part${alphabet[$i]}"
            log "Downloading part ${alphabet[$i]} from URL: $url using proxy: $proxy, range: $range"
            download_with_proxy "$url" "$proxy" "$output" "$range" &
        done
        wait

        # Check and merge parts
        total_size=0
        for ((i=0; i<PROXY_COUNT; i++)); do
            part_file="${DOWNLOAD_PATH}/part${alphabet[$i]}"
            if [ -f "$part_file" ]; then
                part_size=$(stat -c%s "$part_file")
                log "Part ${alphabet[$i]}: size $part_size bytes"
                total_size=$((total_size + part_size))
            else
                log "Part ${alphabet[$i]} was not downloaded"
            fi
        done

        if [ "$total_size" -ne "$file_size" ]; then
            log "Part size ($total_size) does not match file size ($file_size). Deleting parts and retrying."
            rm -f "${DOWNLOAD_PATH}/part*"
            partial_mode
        else
            log "Merging parts into final file."
            cat "${DOWNLOAD_PATH}/part"* > "${DOWNLOAD_PATH}/$(basename $url)"
            rm -f "${DOWNLOAD_PATH}/part*"
        fi
    done < URLs.txt
}

# Concurrent mode
concurrent_mode() {
    while IFS= read -r url; do
        for ((i=0; i<PROXY_COUNT; i++)); do
            proxy="127.0.0.1:$((5000 + i))"
            output="${DOWNLOAD_PATH}/$(basename $url)"
            log "Downloading URL: $url using proxy: $proxy"
            download_with_proxy "$url" "$proxy" "$output" &
        done
    done < URLs.txt
    wait
}

# Mode selection
case $MODE in
    partial)
        partial_mode
        ;;
    concurrent)
        concurrent_mode
        ;;
    *)
        echo "Unknown mode: $MODE"
        exit 1
        ;;
esac
