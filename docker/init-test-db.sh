#!/bin/sh
# Runs once, only on a fresh data volume (Postgres's own
# docker-entrypoint-initdb.d convention). Creates a second, empty database
# alongside the main one so the test suite never shares a database with real
# ingested data.
set -e

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-EOSQL
    CREATE DATABASE "${POSTGRES_DB}_test";
EOSQL
