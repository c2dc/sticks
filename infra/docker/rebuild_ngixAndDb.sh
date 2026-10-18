#!/bin/bash
docker compose stop nginx db
docker compose rm -f nginx db
docker compose up -d --build nginx db
