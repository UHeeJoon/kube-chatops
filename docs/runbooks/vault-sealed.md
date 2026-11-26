# Vault sealed (비밀 값 동기화 실패)

노드 재부팅이나 Vault 파드 재시작 뒤에 생긴다.

## 증상

- ExternalSecret 이 `SecretSyncedError` 로 바뀌고 Ready 가 False 다.
- 새로 뜨는 파드가 Secret 이 없어 시작하지 못한다. 이미 떠 있는 파드는 기존 Secret 으로 계속 돈다.
- Vault 파드는 Running 이지만 Ready 가 아니다 (0/1).

## 확인

- Vault 상태: `kubectl -n security exec vault-0 -- vault status` 의 `Sealed` 값.
- ExternalSecret 상태: `kubectl get externalsecret -A` 에서 Ready 가 False 인 것.
- ESO 로그의 `Vault is sealed` 또는 503.

## 원인

- Vault 는 재시작하면 잠긴(sealed) 상태로 뜬다. 자동 unseal 을 쓰지 않으면 사람이 unseal 키로 풀어야 한다.

## 조치

- 사람이 unseal 한다. 에이전트와 자동화는 unseal 키를 다루지 않는다 (키는 클러스터 밖에 보관).
- unseal 뒤 ExternalSecret 은 다음 동기화 주기(1시간)에 복구된다. 급하면 ExternalSecret 에 `force-sync` 주석을 붙여 바로 동기화한다.
- 보고서에는 "Vault sealed, 사람의 unseal 필요" 와 영향받는 ExternalSecret 목록을 적는다.
