#!/usr/bin/env bash
# Salt okunur sunucu envanteri (Docker, ag, kaynak sinirlari, dinleyen portlar).
# HICBIR SEY DEGISTIRMEZ. Ortam degiskeni, parola, anahtar ya da .env icerigi YAZDIRMAZ:
# docker inspect cikisi yalnizca belirli alanlara bakar (Config.Env kullanilmaz).
# Kullanim (sunucuda):  bash inventory_readonly.sh > /tmp/envanter.txt   ve dosyayi paylasin.
set -u

section() { printf '\n== %s\n' "$1"; }

section "host"
hostname
uname -sr
nproc 2>/dev/null | sed 's/^/cpu: /'
free -h 2>/dev/null | head -3
df -h / /opt 2>/dev/null

section "konteynerler (tum, calisan ve durmus)"
docker ps -a --format '{{.Names}} | {{.Image}} | {{.Status}} | portlar: {{.Ports}}' 2>&1

section "docker aglari"
docker network ls --format '{{.Name}} | {{.Driver}} | {{.Scope}}' 2>&1

section "her agdaki konteynerler (ad ve IP)"
for n in $(docker network ls --format '{{.Name}}' 2>/dev/null); do
  echo "-- ag: $n"
  docker network inspect "$n" --format '{{range .Containers}}   {{.Name}} {{.IPv4Address}}{{"\n"}}{{end}}' 2>&1
done

section "konteyner kaynak sinirlari ve yayimlanan portlar"
docker ps -a --format '{{.Names}}' 2>/dev/null | while read -r c; do
  [ -n "$c" ] || continue
  docker inspect "$c" --format \
    '{{.Name}} | bellek_siniri={{.HostConfig.Memory}} | cpu_nano={{.HostConfig.NanoCpus}} | restart={{.HostConfig.RestartPolicy.Name}} | yayin={{json .NetworkSettings.Ports}}' 2>&1
done

section "host'ta dinleyen portlar (yalnizca port numaralari)"
ss -ltn 2>/dev/null | awk 'NR>1 {print $4}' | sort -u

section "systemd: superbi servisi (anahtar/parola satirlari filtrelenir)"
systemctl is-active superbi 2>&1
systemctl cat superbi 2>/dev/null | grep -v -i -E 'key|secret|password|token|passwd' | head -40

section "postgres-ortak: veritabani adlari ve baglanti siniri (yalnizca okuma)"
docker exec postgres-ortak psql -U postgres -tAc 'SHOW max_connections;' 2>&1
docker exec postgres-ortak psql -U postgres -tAc 'SELECT datname FROM pg_database WHERE NOT datistemplate ORDER BY 1;' 2>&1

section "mysql-ortak: yalnizca konteyner durumu"
docker ps -a --filter name=mysql --format '{{.Names}} | {{.Status}}' 2>&1
