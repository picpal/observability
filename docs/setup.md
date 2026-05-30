# Prometheus + Grafana 운영 가이드 (issue #89)

message-gate 의 관측 가능성 스택 — **Spring Boot Actuator + Micrometer + Prometheus + Grafana**.
사내 폐쇄망 표준 스택을 따른다 (Pinpoint Collector + HBase 신규 구축 비용 회피, 이슈 #85/#86 close 후 방향 전환).

분산 트레이싱(OpenTelemetry + Tempo)은 Phase 2 — 본 문서 범위 밖.

---

## 1. 아키텍처 한눈에

```
┌─────────────────────────────┐         ┌──────────────────┐
│ message-gate (per WAS)      │         │ 사내 Prometheus  │
│                             │         │                  │
│  7700 (메인)                │         │  scrape          │
│   ├─ /api/v1/messages       │ ◀───────┤ /actuator/       │
│   ├─ /api/v1/admin          │         │ prometheus       │
│   ├─ /health (permitAll)    │         │ (15s 간격 권장)  │
│   └─ BasicAuthFilter        │         │                  │
│                             │         └─────────┬────────┘
│  9090 (management)          │                   │
│   └─ /actuator/             │ ◀─────────────────┘
│       health, info,         │  내부망 ACL: Prom IP만 허용
│       prometheus            │  (평문 HTTP — TLS 미적용)
└─────────────────────────────┘
                                                  │
                                                  ▼
                                          ┌──────────────────┐
                                          │ Grafana          │
                                          │ (사내 대시보드)  │
                                          └──────────────────┘
```

**격리 모델**: 메인 7700 은 BasicAuthFilter + TLS 1.2(dev/prod). 9090 은 내부망 ACL 만으로
보호 + 평문 HTTP. 사내 Prometheus 가 단순 HTTP 로 scrape 한다. exposure.include 화이트리스트로
민감 엔드포인트(env / heapdump / loggers / beans)는 매핑 자체가 안 됨.

---

## 2. 인프라팀 협업 체크리스트

본 모듈 배포 전 인프라팀과 다음을 합의·반영한다.

### 2.1 방화벽 / ACL

- **9090 inbound**: 사내 Prometheus 인스턴스 IP 만 allow. 그 외 모든 출발지 deny.
- Active-Active 구성이므로 message-gate 인스턴스가 N 개라면 N 개 모두 9090 등록.

### 2.2 Prometheus scrape 등록

사내 Prometheus 의 `prometheus.yml` 에 추가:

```yaml
scrape_configs:
  - job_name: message-gate
    metrics_path: /actuator/prometheus
    scheme: http               # 평문 (9090 은 TLS 미적용)
    scrape_interval: 10s          # 인스턴스당 약 70KB/scrape (gzip 후 ~7KB). 10s 부담은 미미함
    scrape_timeout: 5s
    static_configs:
      - targets:
          - mg-was-1.internal:9090
          - mg-was-2.internal:9090
        labels:
          environment: prod    # dev / prod 라벨로 인스턴스 구분
```

`application` 라벨은 message-gate 가 직접 발행한다 (`management.metrics.tags.application=message-gate`,
`application.yml`).

### 2.3 Grafana 데이터소스

사내 Grafana 에 위 Prometheus 인스턴스를 등록. 별도 인증 없음 (사내망 신뢰 경계).

---

## 3. application.yml 설정 요약

전체는 `src/main/resources/application*.yml` 참조. 핵심:

```yaml
# 공통 (application.yml)
management:
  server:
    port: 9090                          # 메인 7700 과 분리
  endpoints:
    web:
      exposure:
        include: health,info,prometheus # 화이트리스트 — exclude 같이 두지 말 것
      base-path: /actuator
  endpoint:
    health:
      probes: { enabled: true }
      show-details: never               # 사내 상세 정보 노출 차단
  info:
    env: { enabled: false }             # /actuator/info 에서 env 노출 차단
    os: { enabled: false }
  metrics:
    tags:
      application: message-gate         # 다중 인스턴스 라벨
    distribution:
      percentiles-histogram:
        mg.carrier.send: true           # 패널 2 의 histogram_quantile 가 동작하려면 필수
      minimum-expected-value:
        mg.carrier.send: 1ms
      maximum-expected-value:
        mg.carrier.send: 10s
  prometheus:
    metrics:
      export: { enabled: true }

# dev / prod
management:
  server:
    ssl:
      enabled: false                    # 9090 은 평문, 내부망 ACL 의존

# test
management:
  server:
    port: -1                            # 메인 포트 공유, 통합 테스트 컨텍스트 재시작 시 충돌 회피
```

**보안 자동구성 제외** (`MessageGateApplication.java`):

```java
@SpringBootApplication(
    exclude = {
        DataSourceAutoConfiguration.class,
        ManagementWebSecurityAutoConfiguration.class  // 9090 의 기본 HTTP Basic 차단 해제
    })
```

**Actuator 경로 SecurityFilterChain** (`SecurityConfig.java`):

별도 `actuatorSecurityFilterChain` Bean 으로 `/actuator/**` 경로의 BasicAuthFilter 우회 +
permitAll. `EndpointRequest.toAnyEndpoint()` 매처는 child management context 의 endpoint 를
못 봐서 동작하지 않으므로 plain path matcher 를 사용한다 (실측 후 확정, 본 PR 참조).

---

## 4. 메트릭 카탈로그

### 4.1 도메인 메트릭 (직접 발행)

| 메트릭 | 타입 | 라벨 | 의미 | 발행 위치 |
|---|---|---|---|---|
| `mg_carrier_send_seconds` | Timer | `carrier`, `messageType`, `result` | 통신사 발송 지연 시간 (서킷 브레이커 포함). result=`success`/`fail` | `CarrierRouter.callThroughBreaker` |
| `mg_auth_cache_total` | Counter | `outcome` | AuthCache 분기 결과. outcome=`hit`/`miss`/`inactive` | `AuthService.tryAuthFromCache` |
| `mg_idempotency_duplicate_total` | Counter | (없음) | TID 중복으로 거부된 요청 수 | `IdempotencyGuardImpl.isDuplicate` |
| `mg_grace_auth_total` | Counter | `password` | Grace period 활성 중 인증 성공. password=`NEW`/`OLD` | `AuthService.authenticate` |

Timer 는 Prometheus 노출 시 `_count` / `_sum` / `_max` 시리즈로 자동 분해된다.

### 4.2 자동 메트릭 (Resilience4j / Spring / JVM)

| 메트릭 | 출처 | 의미 |
|---|---|---|
| `resilience4j_circuitbreaker_state` | Resilience4j | CB 상태 (CLOSED/OPEN/HALF_OPEN) per carrier |
| `resilience4j_circuitbreaker_calls_total{kind="not_permitted"}` | Resilience4j | CB OPEN 으로 차단된 호출 수 |
| `resilience4j_circuitbreaker_calls_total{kind="successful"\|"failed"}` | Resilience4j | CB 가 본 호출 결과 |
| `resilience4j_circuitbreaker_failure_rate` | Resilience4j | 슬라이딩 윈도우 실패율 |
| `http_server_requests_seconds` | Micrometer + Spring | 메인 HTTP 엔드포인트 응답 시간 (controller 별) |
| `hikaricp_connections_*` | Micrometer + Hikari | DB 커넥션 풀 사용량 (per datasource) |
| `jvm_memory_used_bytes`, `jvm_gc_pause_seconds`, ... | Micrometer | 표준 JVM 메트릭 |
| `tomcat_threads_*` | Micrometer | 톰캣 스레드 풀 |

**원칙**: 동일 신호를 두 곳에서 발행하지 않는다. circuit breaker 상태는 Resilience4j 가 권위 —
도메인 메트릭의 `result` 라벨에 `circuit_open` 을 별도로 두지 않는 이유.

---

## 5. Grafana 대시보드 권장 패널

운영자가 한 화면에서 판단 내릴 수 있도록 **4~5 패널로 압축**.

### 패널 1: 통신사 발송 성공률 (1d)

```
sum by (carrier) (rate(mg_carrier_send_seconds_count{result="success"}[5m]))
/
sum by (carrier) (rate(mg_carrier_send_seconds_count[5m]))
```

- 시각화: Time series (per-carrier 라인)
- 임계: 95% 미만 시 주의, 90% 미만 시 경보

### 패널 2: 통신사 발송 지연 시간 p95 / p99

```
histogram_quantile(0.95, sum by (carrier, le) (rate(mg_carrier_send_seconds_bucket[5m])))
histogram_quantile(0.99, sum by (carrier, le) (rate(mg_carrier_send_seconds_bucket[5m])))
```

- 시각화: Time series (per-carrier × p95/p99)
- 정상: KT/LG p95 < 500ms

### 패널 3: CircuitBreaker 상태

```
resilience4j_circuitbreaker_state
```

- 시각화: State timeline (per carrier, CLOSED/OPEN/HALF_OPEN 색상 구분)
- OPEN 진입 시점이 한눈에 보이게 함

### 패널 4: AuthCache 효율

```
sum(rate(mg_auth_cache_total{outcome="hit"}[5m]))
/
sum(rate(mg_auth_cache_total[5m]))
```

- 시각화: Stat (hit ratio 단일 숫자) + 보조 line (inactive 분리)
- 정상: hit ratio > 90% (TTL 300초 환경에서)

### 패널 5: Grace period 인증 패턴

```
sum by (password) (rate(mg_grace_auth_total[5m]))
```

- 시각화: Time series (NEW / OLD 두 라인)
- OLD 가 grace 종료 1~2일 전에도 활발하면 운영팀 액션

### 보조 (필요 시): Idempotency 충돌

```
rate(mg_idempotency_duplicate_total[5m])
```

평소 0 이어야 한다. 0 을 넘으면 클라이언트 재시도 패턴 또는 TID 충돌 조사.

### 5.7 per-instance 변형 (운영 권장: Active-Active N대 비교)

운영은 Active-Active 2대 이상이므로 인스턴스를 합산하지 말고 분리해서 본다. 한쪽 WAS 만
지연/실패가 튀는 케이스를 즉시 잡기 위함. 합산 버전(위 §5.1~§5.6)은 잘못된 평균에 가려져
한쪽 인스턴스의 이상 징후를 못 본다.

| 패널 | 분리 PromQL | 비고 |
|---|---|---|
| 1 성공률 | `sum by (instance, carrier) (rate(mg_carrier_send_seconds_count{result="success"}[5m])) / clamp_min(sum by (instance, carrier) (rate(mg_carrier_send_seconds_count[5m])), 1e-12)` | legend `{{instance}} / {{carrier}}` |
| 2 p95 | `histogram_quantile(0.95, sum by (instance, carrier, le) (rate(mg_carrier_send_seconds_bucket[5m])))` | p99 도 동일 패턴 |
| 3 CB state | `resilience4j_circuitbreaker_state` (`instance` 라벨이 자동 포함됨 — 추가 작업 불필요) | State timeline 의 row 가 instance × name × state 로 분리 |
| 4 AuthCache hit ratio | `sum by (instance) (rate(mg_auth_cache_total{outcome="hit"}[5m])) / clamp_min(sum by (instance) (rate(mg_auth_cache_total[5m])), 1e-12)` | Caffeine 은 인스턴스 로컬 — 분리 의미가 큼 |
| 4-보조 outcome 분해 | `sum by (instance, outcome) (rate(mg_auth_cache_total[5m]))` | per-instance × hit/miss/inactive |
| 5 Grace | `sum by (instance, password) (rate(mg_grace_auth_total[5m]))` | 패턴 분포가 인스턴스마다 다른지 확인 |
| 6 Idempotency | `sum by (instance) (rate(mg_idempotency_duplicate_total[5m]))` | 평소 0, 한쪽 인스턴스만 튀면 LB sticky 문제 후보 |

#### legend 단축 — `instance` 가 `mg-was-1.internal:9090` 처럼 길면

prometheus.yml 의 `relabel_configs` 로 호스트 부분만 추출.

```yaml
scrape_configs:
  - job_name: message-gate
    metrics_path: /actuator/prometheus
    scrape_interval: 10s
    static_configs:
      - targets:
          - mg-was-1.internal:9090
          - mg-was-2.internal:9090
        labels:
          environment: prod
    relabel_configs:
      # mg-was-1.internal:9090 → was-1
      - source_labels: [__address__]
        regex: "mg-was-([0-9]+)\\..+"
        replacement: was-$1
        target_label: instance
```

→ Grafana legend 가 `was-1 / KT`, `was-2 / LG` 같이 짧게 표시. 원본 `__address__` 는 scrape job
이 내부적으로 사용하므로 relabel 후에도 scrape 자체에는 영향 없음.

---

## 5-A. 다른 서비스의 WAS 그룹 추가 — `job_name` 분리

Prometheus 에는 "그룹"이라는 명시적 객체가 없다. 대신 `scrape_configs[]` 의 각 항목이
한 **job**이며 = 한 서비스 단위로 본다. 같은 서비스의 인스턴스들은 같은 job 안에
`targets[]` 로 묶고, **다른 서비스는 별도 job** 으로 분리한다.

```yaml
scrape_configs:
  - job_name: message-gate              # 서비스 A
    metrics_path: /actuator/prometheus
    scrape_interval: 10s
    static_configs:
      - targets:
          - mg-was-1.internal:9090
          - mg-was-2.internal:9090
        labels:
          environment: prod

  - job_name: other-service             # 서비스 B (예: order-gate)
    metrics_path: /actuator/prometheus
    scrape_interval: 10s
    static_configs:
      - targets:
          - other-was-1.internal:9090
          - other-was-2.internal:9090
        labels:
          environment: prod
```

이렇게 두면 모든 시리즈에 `job="<서비스명>"` 라벨이 자동으로 붙는다. Grafana 에서
서비스 필터링은 `{job="message-gate"}` 식으로 분리. 한 Grafana 인스턴스에서 서비스별
폴더(`Dashboards/message-gate`, `Dashboards/order-gate`) 로 대시보드를 분리하는 운영도 자연스러움.

**메트릭 이름 중복 주의** — 서로 다른 서비스가 `http_server_requests_seconds` 같은 표준 이름을
모두 발행한다. 이름만으로 쿼리하면 모든 서비스 데이터가 섞이므로, 대시보드 쿼리에는 항상
`{job="..."}` 또는 `{application="..."}` 필터를 명시한다 (message-gate 는 두 라벨이 동일 값으로
발행되므로 어느 쪽을 써도 됨).

### 5-A.1 신규 서비스를 붙이는 절차 (step-by-step)

신규 서비스 `order-gate` 의 Active-Active 2대(`order-was-1`, `order-was-2`) 를 같은
Prometheus + Grafana 에 등록하는 경우를 기준으로 한다.

#### Step 1 — Prometheus 측 설정

**1-1) `prometheus.yml` 에 신규 job 항목 추가** (기존 message-gate 항목은 그대로 둔다).

```yaml
scrape_configs:
  - job_name: message-gate
    metrics_path: /actuator/prometheus
    scrape_interval: 10s
    static_configs:
      - targets:
          - mg-was-1.internal:9090
          - mg-was-2.internal:9090
        labels:
          environment: prod

  - job_name: order-gate                  # ★ 신규
    metrics_path: /actuator/prometheus
    scrape_interval: 10s
    static_configs:
      - targets:
          - order-was-1.internal:9090
          - order-was-2.internal:9090
        labels:
          environment: prod
```

**1-2) Prometheus 설정 리로드** (재기동 없이).

```bash
# --web.enable-lifecycle 옵션으로 띄운 경우
curl -X POST http://prom-server.internal:9090/-/reload

# 옵션 없이 띄운 경우엔 SIGHUP
sudo kill -HUP $(pidof prometheus)
```

**1-3) Targets 페이지에서 health 확인** — `http://prom-server.internal:9090/targets`.
새 job 의 모든 target 이 `UP` 인지, scrape duration 이 timeout 안인지 확인.

**1-4) 사내 ACL 확인** — 신규 서비스의 9090 도 Prometheus IP 만 도달 가능해야 한다.

#### Step 2 — Grafana 측 설정

**2-1) 데이터소스는 추가하지 않는다** — 같은 Prometheus 인스턴스를 그대로 사용한다.
신규 서비스는 새 `job` 라벨로만 구분되며, Grafana 측에서는 쿼리 필터만 분리하면 된다.

**2-2) 대시보드 폴더 분리** — Grafana provisioning 의 dashboards provider 를 서비스별로
분리하면 사이드바에 폴더가 생긴다. 사내 Grafana 의 provisioning 디렉터리 (`/etc/grafana/provisioning/dashboards/`) 에 다음을 추가.

```yaml
# /etc/grafana/provisioning/dashboards/order-gate.yml
apiVersion: 1
providers:
  - name: order-gate
    orgId: 1
    folder: "order-gate"
    type: file
    disableDeletion: true
    updateIntervalSeconds: 10
    options:
      path: /var/lib/grafana/dashboards/order-gate
      foldersFromFilesStructure: false
```

대시보드 JSON 은 `/var/lib/grafana/dashboards/order-gate/*.json` 에 배치한다.

**2-3) 대시보드 쿼리에 `{job="order-gate"}` 필터 강제** — `http_server_requests_seconds`,
`jvm_memory_used_bytes` 같은 표준 이름은 모든 서비스가 동일 이름으로 발행하므로 필터 없이 쿼리하면 데이터가 섞인다. message-gate 의 도메인 메트릭(`mg_*`) 은 prefix 가 unique 하므로 필터 생략해도 안전하지만, 일관성을 위해 신규 서비스 대시보드는 모든 패널 쿼리에 명시한다.

```promql
# 좋은 예 (서비스 격리)
sum by (instance) (rate(http_server_requests_seconds_count{job="order-gate"}[5m]))

# 나쁜 예 (전 서비스 합산되어 의미 불명)
sum by (instance) (rate(http_server_requests_seconds_count[5m]))
```

**2-4) Grafana 재기동 또는 provisioning 자동 재적용 대기** — `updateIntervalSeconds` 마다 폴더/대시보드 자동 등록된다. 즉시 확인하려면 `systemctl restart grafana-server`.

#### Step 3 — 검증

- Prometheus UI Targets 에서 `order-gate` job 의 모든 target 이 UP.
- Prometheus 쿼리 콘솔에서 `up{job="order-gate"}` 가 모든 instance 에 대해 `1`.
- Grafana 사이드바 Dashboards 메뉴에 `order-gate` 폴더가 보이고, 그 안의 대시보드가 데이터 표시.
- 기존 message-gate 대시보드는 영향 없음을 확인 (job 라벨이 분리되어 있으므로 시리즈가 섞이지 않음).

#### 폴더 구조 요약 (사내 Grafana 호스트 기준)

```
/etc/grafana/provisioning/
├── datasources/
│   └── prometheus.yml                     # 데이터소스 1개로 모든 서비스 공유
└── dashboards/
    ├── message-gate.yml                   # provider: message-gate
    └── order-gate.yml                     # provider: order-gate (신규)

/var/lib/grafana/dashboards/
├── message-gate/
│   └── message-gate-phase1.json
└── order-gate/                            # 신규
    └── order-gate-overview.json
```

---

## 6. Alertmanager 룰 예시

상세 임계는 운영 합의 후 조정. 다음은 출발점.

```yaml
groups:
  - name: message-gate
    interval: 30s
    rules:
      - alert: CarrierFailureRateHigh
        expr: |
          (
            sum by (carrier) (rate(mg_carrier_send_seconds_count{result="fail"}[5m]))
            /
            sum by (carrier) (rate(mg_carrier_send_seconds_count[5m]))
          ) > 0.05
        for: 5m
        labels: { severity: warning }
        annotations:
          summary: "{{ $labels.carrier }} 발송 실패율 5% 초과 (5분 평균)"

      - alert: CircuitBreakerOpen
        expr: resilience4j_circuitbreaker_state{state="open"} == 1
        for: 1m
        labels: { severity: critical }
        annotations:
          summary: "{{ $labels.name }} CircuitBreaker OPEN 진입"

      - alert: AuthCacheMissSpike
        expr: |
          rate(mg_auth_cache_total{outcome="miss"}[5m]) > 50
        for: 5m
        labels: { severity: warning }
        annotations:
          summary: "AuthCache miss 폭증 — DB 부하 확인 필요"

      - alert: IdempotencyDuplicateSpike
        expr: rate(mg_idempotency_duplicate_total[5m]) > 1
        for: 5m
        labels: { severity: warning }
        annotations:
          summary: "TID 중복 거부 발생 — 클라이언트 재시도 또는 TID 충돌 조사"

      - alert: PrometheusScrapeDown
        expr: up{job="message-gate"} == 0
        for: 2m
        labels: { severity: critical }
        annotations:
          summary: "{{ $labels.instance }} 메트릭 수집 중단"
```

---

## 7. 보안 체크리스트

배포 전 반드시 확인.

- [ ] 9090 포트가 사내망 Prometheus IP **만** 도달 가능 (방화벽/ACL 검증).
- [ ] `application.yml` 의 `exposure.include` 가 `health,info,prometheus` 만 포함. `env`, `heapdump`, `loggers`, `beans` 추가 금지.
- [ ] `exposure.exclude` 키를 함께 두지 않을 것 — include 화이트리스트만으로 충분하며, exclude 가 include 보다 우선하여 모든 endpoint 가 404 가 된 적 있음 (본 PR 실측).
- [ ] `info.env.enabled=false`, `info.os.enabled=false` 유지 — 환경변수/호스트 정보 노출 차단.
- [ ] `endpoint.health.show-details=never` 유지.
- [ ] **PII 라벨 금지**: 메트릭 라벨에 `tid`, `loginId`, `phoneNumber`, `messageBody` 등 절대 추가 금지. 새 메트릭 추가 시 코드 리뷰 단계에서 라벨을 점검.
- [ ] 새 카운터/타이머 추가 시 cardinality 폭주 점검 — 라벨 조합이 1000 개를 넘으면 Prometheus 부하.
- [ ] `MessageGateApplication` 의 `ManagementWebSecurityAutoConfiguration` exclude 를 의도 없이 제거하지 말 것 — 제거 시 9090 의 모든 endpoint 가 HTTP Basic 인증 요구로 돌아가 사내 Prometheus 가 401.

### 7.1 Staging Cutover 검증 (dev/prod 프로파일 실측)

`./gradlew clean test` 와 `bootRun --args='--spring.profiles.active=local'` 매트릭스는 H2/평문 조합만 검증한다. dev/prod 는 **main 7700 = TLS 1.2 + 9090 = 평문** 조합이므로 staging WAS 에서 다음을 직접 확인한다.

```bash
# 9090 평문 — 인증 없이 200, Prometheus 포맷
curl -s  -o /dev/null -w "%{http_code}\n" http://mg-was-1.staging:9090/actuator/prometheus    # 기대: 200
curl -s                                  http://mg-was-1.staging:9090/actuator/prometheus | head -3   # 기대: # HELP / # TYPE 헤더

# 9090 화이트리스트 외 endpoint 차단
curl -s  -o /dev/null -w "%{http_code}\n" http://mg-was-1.staging:9090/actuator/env           # 기대: 404
curl -s  -o /dev/null -w "%{http_code}\n" http://mg-was-1.staging:9090/actuator/heapdump      # 기대: 404

# 7700 TLS 메인 — 인증 강제 유지
curl -sk -o /dev/null -w "%{http_code}\n" https://mg-was-1.staging:7700/api/v1/messages       # 기대: 401
curl -sk -o /dev/null -w "%{http_code}\n" https://mg-was-1.staging:7700/actuator/prometheus   # 기대: 404 (main 포트에 actuator 없음)
```

위 5 줄 전부 기대값이 아닐 경우 cutover 중단하고 본 PR 변경을 staging 에서 rollback. dev/prod 의 `management.server.ssl.enabled=false` 가 외부망에 노출되지 않도록 7.1 의 9090 ACL 항목과 짝으로 검증할 것.

---

## 8. 트러블슈팅

| 증상 | 가능한 원인 | 확인 |
|---|---|---|
| 9090 /actuator/prometheus → 401 | `ManagementWebSecurityAutoConfiguration` exclude 누락 또는 main `BasicAuthFilter` 가 9090 에 적용됨 | `SecurityConfig#actuatorSecurityFilterChain` Bean 존재 확인 |
| 9090 /actuator/prometheus → 404 | `exposure.include` 에 prometheus 없음 또는 `exposure.exclude="*"` 가 함께 설정됨 | application.yml 의 management.endpoints.web.exposure 점검 |
| 9090 listen 안 됨 | `management.server.port` 적용 안 됨 (테스트 프로파일은 의도적으로 -1) | 실행 프로파일 확인. `lsof -i :9090` 으로 listen 검증 |
| Prometheus scrape `up == 0` | 방화벽/ACL 또는 인스턴스 다운 | 사내 Prom 에서 직접 `curl http://mg-was-1.internal:9090/actuator/health` |
| 사내 Prometheus 가 scrape 했으나 `application` 라벨 없음 | `management.metrics.tags.application` 미설정 | application.yml 공통 섹션 복원 |
| circuit breaker 메트릭 안 보임 | Resilience4j Micrometer 바인딩 누락 | `resilience4j_circuitbreaker_state` 쿼리. 빈 결과면 actuator 메트릭 endpoint 로 raw 확인 |
| 부팅 시 7700 already in use | 이전 bootRun 좀비 프로세스 | `lsof -i :7700` → kill |

---

## 9. Phase 2 예고 — 분산 트레이싱

도입 트리거: "carrier 장애 시 latency 가 어느 단계에서 터지는지 모르겠다" 는 운영자 호소가 발생할 때.

### 도입 시 예상 구성

- **계측**: OpenTelemetry Java agent (`-javaagent:opentelemetry-javaagent.jar`) — 코드 변경 0.
  - 도입 시 agent 의 servlet 자동 계측에서 `/actuator/**` 경로를 제외할 것 (`-Dotel.instrumentation.common.exclude-paths=/actuator/*` 또는 동등 설정). 사내 Prometheus 의 scrape 1회당 trace 1건이 추가 발생하면 Tempo 부하가 scrape 빈도에 비례해 폭증한다.
- **백엔드**: Grafana Tempo (Prometheus 와 동일 Grafana 생태계 — 운영 일관성).
- **샘플링**: 1% 또는 에러 trace 100% (운영 합의 필요).

본 Phase 1 의 메트릭 (`mg_carrier_send_seconds`) 과 trace 가 `application` / `carrier` 라벨로
조인 가능하므로, Grafana 에서 메트릭 → 트레이스 drill-down 자연스럽게 구성 가능.

---

## 관련 문서

- `runbook-message-gate.md` — **1페이지 운영 런북** (알람 대응, 인스턴스 추가/제거, escalation)
- message-gate 이슈 #89 — Phase 1 도입 PR (앱 측 actuator/메트릭 노출)
- message-gate 이슈 #85, #86 (close) — Pinpoint APM 방향 전환 배경
- 앱 메트릭 명세 — `picpal/message-gate` 레포 `docs/observability/metrics.md`
