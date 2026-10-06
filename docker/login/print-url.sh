#!/bin/sh
# The "browser" sf opens during login-devhub.sh: it hands the login URL back to the script.
echo "$1" >"$LOGIN_URL_FILE"
