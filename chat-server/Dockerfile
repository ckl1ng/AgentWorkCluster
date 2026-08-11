# Multi-stage build for Chat Server
# Stage 1: Build
FROM rust:1.75-slim-bookworm AS builder

RUN apt-get update && apt-get install -y --no-install-recommends \
    pkg-config \
    libssl-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy manifests first for dependency caching
COPY Cargo.toml Cargo.lock ./
RUN mkdir src && echo "fn main() {}" > src/main.rs
RUN cargo build --release 2>/dev/null || true
RUN rm -rf src

# Copy actual source
COPY src/ ./src/

# Build release binary
RUN cargo build --release

# Stage 2: Runtime
FROM debian:bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy binary from builder
COPY --from=builder /app/target/release/chat-server /app/chat-server

# Create data directory
RUN mkdir -p /data

EXPOSE 9010

ENV HOST=0.0.0.0
ENV PORT=9010
ENV DATA_DIR=/data

VOLUME ["/data"]

CMD ["/app/chat-server"]
