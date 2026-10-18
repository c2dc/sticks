#!/bin/bash

systemctl stop docker
rm -rf /var/lib/docker
systemctl start Docker

docker compose up --build
