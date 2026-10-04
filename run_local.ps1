# Arranca Django y el gateway Node.js para pruebas locales.
# Uso: .\run_local.ps1 [-Lan]   (-Lan permite abrirlo desde el teléfono en la misma red)
param([switch]$Lan)
$bind = if ($Lan) { "0.0.0.0:8000" } else { "127.0.0.1:8000" }
$env:GATEWAY_URL = "http://127.0.0.1:3000"
$env:DJANGO_URL = "http://127.0.0.1:8000"
python manage.py migrate
$gateway = Start-Process node -ArgumentList "src/server.js" -WorkingDirectory "$PSScriptRoot\gateway" -PassThru -NoNewWindow
try { python manage.py runserver $bind } finally { Stop-Process -Id $gateway.Id -ErrorAction SilentlyContinue }
