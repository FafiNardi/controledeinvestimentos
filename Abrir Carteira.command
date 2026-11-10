#!/bin/bash
# Abre o programa de Carteira de Investimentos
cd "$(dirname "$0")"
echo "Iniciando a Carteira de Investimentos..."
# abre o navegador depois de 2s
( sleep 2 && open "http://127.0.0.1:5001" ) &
./venv/bin/python app.py
