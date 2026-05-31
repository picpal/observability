# observability

사내 서비스의 Prometheus scrape 설정과 Grafana 대시보드를 모아두는 레포.
관측 인프라(수집·시각화)는 여기서, **메트릭 정의(이름·라벨·의미)는 각 서비스 레포** 의 `docs/observability/metrics.md` 에서 관리한다.

현재 등록 서비스:

| 서비스 | 앱 레포 | 메트릭 명세 |
|---|---|---|
| message-gate | `picpal/message-gate` | `docs/observability/metrics.md` |

## 구조

```
observability/
├── docker-compose.yml                          # 로컬 검수 stack (Prometheus + Grafana)
├── docker-compose.prod.yml                     # 폐쇄망 운영 stack (실인증·영속볼륨·retention)
├── prometheus/
│   ├── prometheus.yml                          # 로컬 검수 (host.docker.internal scrape)
│   └── prometheus.prod.example.yml             # 운영 템플릿 (message-gate + nginx job)
├── grafana/
│   ├── provisioning/
│   │   ├── datasources/prometheus.yml          # 자동 등록 (uid: prometheus)
│   │   └── dashboards/dashboards.yml           # 자동 등록 (folder: message-gate)
│   └── dashboards/
│       ├── message-gate.json                   # per-instance 비교 대시보드
│       ├── jvm.json                            # JVM 런타임 (actuator/Micrometer)
│       └── nginx.json                          # nginx stub_status 대시보드
└── docs/
    ├── setup.md                                # 전체 설계 + 운영 가이드
    ├── install-airgap.md                       # 폐쇄망 반입 + 설치 가이드 (Monitoring VM)
    ├── security-review.md                      # 폐쇄망 반입 보안심의 근거 자료
    └── runbook-message-gate.md                 # 1페이지 운영 런북
```

## 로컬 검수 (개발자)

대상 앱(message-gate 등) 이 `9090/actuator/prometheus` 를 노출하고 있어야 한다.

```bash
docker compose up -d
open http://localhost:3001          # Grafana — admin / admin
open http://localhost:9091          # Prometheus
```

stop & clean:

```bash
docker compose down -v
```

## 운영 적용 (폐쇄망 Monitoring VM)

운영 환경의 "사내 Prometheus/Grafana" 는 **온프렘 폐쇄망 Monitoring VM 1대에 `docker-compose.prod.yml`**
로 구축한다. 반입(docker save/load)·설치·App 서버 nginx exporter 설치·방화벽까지 전 과정은
**[`docs/install-airgap.md`](docs/install-airgap.md)** 참조. 요약:

1. 인터넷 호스트에서 이미지 번들 생성(`docker save`) → 승인 매체로 반입 → Monitoring VM 에서 `docker load`.
2. `prometheus.prod.example.yml` → `prometheus.prod.yml` 복사 후 `targets[]` 를 실제 WAS 호스트로 교체
   (message-gate 9090 + nginx exporter 9113). 각 WAS 의 OS 방화벽은 Monitoring VM IP 만 inbound allow.
3. `docker compose -f docker-compose.prod.yml up -d` — 대시보드/datasource 는 provisioning 으로 자동 등록.
4. 알람 룰 (alertmanager) 은 `docs/setup.md §6` 참고.

## 신규 서비스 추가 절차

`docs/setup.md §5-A.1` 의 step-by-step 가이드 참고. 요약:

1. **앱 측** — 9090 포트 분리 + `/actuator/prometheus` 노출 + `docs/observability/metrics.md` 작성.
2. **observability 측** — `prometheus.yml` 에 새 `job_name` block 추가 (메트릭 이름 충돌 주의).
3. **Grafana** — 새 대시보드 JSON 을 `grafana/dashboards/` 에 추가, `{job="..."}` 필터로 서비스 격리.

## 트레이드오프 메모

- 대시보드 JSON 은 **인프라 레포(여기)** 가 owner. 앱 메트릭 이름이 바뀌면 cross-repo PR 필요.
- 앱 메트릭 명세 는 **각 앱 레포** 가 owner. observability 레포는 그 명세를 참조해 패널/룰 구성.
- 두 레포가 동시 변경되는 경우는 메트릭 rename/추가/삭제 시점이며, 보통 앱 레포 PR 먼저 머지 후 인프라 레포 PR.
