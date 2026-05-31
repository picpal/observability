# 폐쇄망 반입 + 설치 가이드 (Monitoring VM)

온프렘 IDC 폐쇄망에 **Prometheus + Grafana 모니터링 스택**을 docker compose 로 구축하는 절차.
인터넷이 닿는 준비 호스트에서 **번들을 만들어 승인 매체로 반입**한 뒤, 폐쇄망 안에서 설치한다.

본 문서는 *반입·설치* 절차에 집중한다. 메트릭 의미·대시보드·알람·앱(actuator) 설정은
[`setup.md`](setup.md), 일상 대응은 [`runbook-message-gate.md`](runbook-message-gate.md) 참조.

---

## 0. 대상 구성 (온프렘, 같은 망)

```
[IDC 내부망 — 동일 서브넷]

  mg-was-1.internal                       mg-was-2.internal
   ├ message-gate WAS  :9090 (actuator)    ├ message-gate WAS  :9090
   ├ nginx             (web)               ├ nginx             (web)
   └ nginx-exporter    :9113               └ nginx-exporter    :9113
        ▲   ▲                                   ▲   ▲
        │   └──────────────┐         ┌──────────┘   │
   OS 방화벽: Monitoring VM IP 만 9090/9113 inbound allow
        │                  │         │              │
        └──────────── [ Monitoring VM ] ────────────┘
                       docker compose
                       ├ prometheus :9090 (VM 로컬 바인딩, scrape 수행)
                       └ grafana    :3000 (사내망 노출, 대시보드)
```

- **scrape 방향**: Monitoring VM → 각 WAS (pull). 따라서 방화벽은 **WAS 쪽 inbound** 를 연다.
- **반입 대상**: docker 이미지 3종 + 이 레포의 설정 파일. (Grafana 외부 플러그인 없음 → 플러그인 번들 불필요)

---

## 1. 전제조건

| 항목 | 확인 |
|---|---|
| Monitoring VM 에 docker / docker compose v2 설치됨 | `docker version`, `docker compose version` |
| App 서버 2대에서 nginx 가 `stub_status` 모듈 포함 빌드 | `nginx -V 2>&1 \| grep -o with-http_stub_status_module` |
| App 서버에 docker 또는 exporter 바이너리 실행 권한 | (nginx-exporter 기동 방식 결정, §5) |
| Monitoring VM ↔ WAS 가 동일 내부망 | 네트워크팀 룰 신청 불필요, OS 방화벽만 |
| 반입 승인 매체 + 반입 절차 | 사내 보안 정책 |

> docker 엔진 자체가 폐쇄망 VM 에 없다면 그 설치(오프라인 rpm/deb 또는 사내 미러)가 **선행 과제**다.
> 본 가이드 범위 밖 — 인프라팀과 별도 협의.

---

## 2. [인터넷 호스트] 반입 번들 생성

인터넷이 닿는 준비용 리눅스 호스트(아키텍처는 폐쇄망 VM 과 동일해야 함 — 보통 `linux/amd64`)에서 수행.

### 2.1 이미지 pull + save

```bash
mkdir -p mg-observability-bundle/images && cd mg-observability-bundle

# 버전은 docker-compose.prod.yml 에 핀된 것과 일치시킨다.
# nginx-exporter 최신 안정 (확인일 기준 1.5.1 — 반입 시점 최신 태그 재확인 권장)
PROM=prom/prometheus:v2.54.1
GRAF=grafana/grafana:11.2.2
NGEX=nginx/nginx-prometheus-exporter:1.5.1

for IMG in "$PROM" "$GRAF" "$NGEX"; do
  docker pull --platform linux/amd64 "$IMG"
done

# 단일 tar 로 저장 (load 시 태그 그대로 복원됨)
docker save "$PROM" "$GRAF" "$NGEX" -o images/observability-images.tar
```

### 2.2 레포 설정 파일 동봉

```bash
# observability 레포를 clone 또는 export 해서 config 만 복사
git clone <observability-repo-url> repo
cp -r repo/docker-compose.prod.yml \
      repo/prometheus \
      repo/grafana \
      repo/docs \
      ./
rm -rf repo
```

### 2.3 무결성 체크섬 + 패키징

```bash
cd ..
sha256sum mg-observability-bundle/images/observability-images.tar > bundle.sha256
tar czf mg-observability-bundle.tar.gz mg-observability-bundle/
sha256sum mg-observability-bundle.tar.gz >> bundle.sha256
cat bundle.sha256        # 반입 후 검증에 사용 — 이 값을 따로 기록/출력
```

→ `mg-observability-bundle.tar.gz` 와 `bundle.sha256` 을 승인 매체로 반입.

---

## 3. [반입 후] 무결성 검증

폐쇄망 Monitoring VM 에 매체를 연결 후:

```bash
# 반입 전 기록한 sha256 과 대조
sha256sum -c bundle.sha256        # : OK 두 줄 확인
tar xzf mg-observability-bundle.tar.gz
cd mg-observability-bundle
```

값이 다르면 매체/전송 손상 — **설치 중단하고 재반입.**

---

## 4. [Monitoring VM] 스택 설치

### 4.1 이미지 load

```bash
docker load -i images/observability-images.tar
docker images | grep -E 'prometheus|grafana|nginx-prometheus-exporter'   # 3개 확인
```

### 4.2 운영 설정 파일 준비

```bash
# Prometheus target 을 실제 호스트로 — 템플릿을 복사해 편집
cp prometheus/prometheus.prod.example.yml prometheus/prometheus.prod.yml
vi prometheus/prometheus.prod.yml
#   message-gate job targets[] → mg-was-1.internal:9090, mg-was-2.internal:9090
#   nginx       job targets[] → mg-was-1.internal:9113, mg-was-2.internal:9113

# Grafana admin 비밀번호 (.env — 커밋/반출 금지)
cat > .env <<'EOF'
GF_SECURITY_ADMIN_PASSWORD=<강력한_비밀번호>
EOF
chmod 600 .env
```

### 4.3 기동

```bash
docker compose -f docker-compose.prod.yml up -d
docker compose -f docker-compose.prod.yml ps        # prometheus, grafana 둘 다 Up
```

- Grafana: `http://<monitoring-vm>:3000` → admin / (.env 비밀번호). datasource·대시보드는 provisioning 으로 자동 등록.
- Prometheus 는 VM 로컬(127.0.0.1:9090) 바인딩 — 직접 보려면 SSH 터널:
  `ssh -L 9090:localhost:9090 <monitoring-vm>` 후 `http://localhost:9090/targets`.

---

## 5. [App 서버 2대] nginx 메트릭 노출

각 WAS 호스트(`mg-was-1`, `mg-was-2`)에서 동일하게 수행.

### 5.1 nginx `stub_status` 활성화

nginx 설정에 로컬 전용 status 엔드포인트 추가:

```nginx
# /etc/nginx/conf.d/stub_status.conf
server {
    listen 127.0.0.1:8080;
    location /stub_status {
        stub_status;
        allow 127.0.0.1;
        deny all;            # 외부 직접 접근 차단 — exporter 만 로컬로 읽음
    }
}
```

```bash
nginx -t && nginx -s reload
curl -s http://127.0.0.1:8080/stub_status      # Active connections: ... 출력 확인
```

> **포트 충돌 주의**: App 서버엔 이미 nginx(80/443) + WAS(7700/9090)가 떠 있다. 8080 이 다른
> 로컬 서비스에 점유돼 있으면(`ss -ltn '( sport = :8080 )'` 확인) 비어 있는 로컬 포트로 바꾸고,
> §5.2 exporter 의 `--nginx.scrape-uri` 도 같은 포트로 맞춘다.

### 5.2 nginx-prometheus-exporter 기동 (9113)

**방법 A — docker (이미지 반입했을 때, 권장)**

```bash
# 번들의 이미지를 이 서버에도 load (또는 사내 레지스트리에서 pull)
docker load -i observability-images.tar     # nginx-exporter 포함

docker run -d --name nginx-exporter --restart unless-stopped \
  --network host \
  nginx/nginx-prometheus-exporter:1.5.1 \
  --nginx.scrape-uri=http://127.0.0.1:8080/stub_status
```

**방법 B — 바이너리 + systemd (App 서버에 docker 없을 때)**

```bash
# 반입한 nginx-prometheus-exporter 바이너리를 /usr/local/bin/ 에 배치 후
cat > /etc/systemd/system/nginx-exporter.service <<'EOF'
[Unit]
Description=nginx-prometheus-exporter
After=network.target nginx.service
[Service]
ExecStart=/usr/local/bin/nginx-prometheus-exporter \
  --nginx.scrape-uri=http://127.0.0.1:8080/stub_status \
  --web.listen-address=:9113
Restart=always
[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload && systemctl enable --now nginx-exporter
```

```bash
curl -s http://127.0.0.1:9113/metrics | grep '^nginx_'    # nginx_up 등 확인
```

### 5.3 방화벽 — Monitoring VM IP 만 허용

WAS actuator(9090)와 nginx-exporter(9113) 둘 다 Monitoring VM 출발지만 허용 (firewalld 예):

```bash
MON_IP=<monitoring-vm-ip>
for P in 9090 9113; do
  firewall-cmd --permanent --add-rich-rule="rule family=ipv4 \
    source address=${MON_IP} port port=${P} protocol=tcp accept"
done
firewall-cmd --reload
firewall-cmd --list-rich-rules        # 두 규칙 확인
```

> 9113 은 외부에 열 필요 없는 메트릭 포트다. **source 를 Monitoring VM 으로 한정**하는 것이 핵심.
> stub_status(8080)는 127.0.0.1 바인딩이라 방화벽 대상조차 아님.

---

## 6. 설치 검증 (end-to-end)

```bash
# 1) Monitoring VM → WAS 도달 (방화벽 OK 확인)
curl -s -m 3 http://mg-was-1.internal:9090/actuator/health   # {"status":"UP"...}
curl -s -m 3 http://mg-was-1.internal:9113/metrics | head -3 # # HELP nginx_...

# 2) Prometheus targets 전부 UP (SSH 터널 후)
#    http://localhost:9090/targets → message-gate(2), nginx(2) 모두 UP
curl -s http://localhost:9090/api/v1/query?query=up | \
  python3 -c "import sys,json;[print(r['metric'].get('job'),r['metric'].get('instance'),r['value'][1]) for r in json.load(sys.stdin)['data']['result']]"
#    → 모든 행의 마지막 값이 1
```

Grafana(`:3000`) 로그인 → 좌측 Dashboards 에 message-gate / nginx 대시보드가 보이고
패널에 데이터가 차면 완료.

체크리스트:

- [ ] 번들 sha256 검증 OK
- [ ] 이미지 3종 load 확인
- [ ] `prometheus.prod.yml` targets 실제 호스트로 교체
- [ ] `.env` 비밀번호 설정 (chmod 600), 익명 접근 비활성 확인 (로그인 폼 뜸)
- [ ] App 서버 2대: stub_status 200 + exporter 9113 응답
- [ ] App 서버 2대: 9090/9113 방화벽 Monitoring VM IP 한정
- [ ] Prometheus `/targets` 4개 모두 UP
- [ ] Grafana 대시보드 데이터 표시

---

## 7. 운영 (폐쇄망)

| 작업 | 절차 |
|---|---|
| **설정 변경**(target 추가 등) | `prometheus.prod.yml` 편집 → `curl -X POST http://localhost:9090/-/reload` (재기동 불필요, lifecycle 활성) |
| **WAS 인스턴스 증설** | `prometheus.prod.yml` 의 `targets[]` 에 호스트 추가 + 그 서버에 §5 반복 → reload |
| **버전 업그레이드** | §2 와 동일하게 새 이미지 번들 반입 → `docker load` → compose 의 이미지 태그 수정 → `up -d` (volume 유지되므로 데이터 보존) |
| **백업** | named volume `prometheus-data`(TSDB), `grafana-data`(대시보드 편집분/사용자) 를 `docker run --rm -v <vol>:/v -v $PWD:/b alpine tar czf /b/<vol>.tgz -C /v .` 로 주기 백업 |
| **데이터 보존기간 조정** | compose 의 `--storage.tsdb.retention.time` 수정 후 `up -d` |
| **중지(데이터 유지)** | `docker compose -f docker-compose.prod.yml down` — **`-v` 금지** (붙이면 볼륨 삭제로 메트릭/대시보드 소실) |

---

## 관련 문서

- [`setup.md`](setup.md) — 전체 설계, 메트릭 카탈로그, 대시보드 패널, 알람 룰, 보안 체크리스트
- [`runbook-message-gate.md`](runbook-message-gate.md) — 1페이지 운영 런북
- `prometheus/prometheus.prod.example.yml` — scrape 설정 템플릿
- `docker-compose.prod.yml` — 운영 스택 정의
