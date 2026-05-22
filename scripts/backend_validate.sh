#!/bin/bash
set -e

echo "Validating backend..."

curl -f http://localhost/api/health > /dev/null

echo "Backend validation passed."