# 佳駿警考 AI｜司法院判決索引

這個 Repository 負責把司法院歷史裁判書匯入 Supabase，建立中文全文搜尋、判決號精確搜尋、中文語意向量索引，並提供 RAG 檢索來源給後續 AI 法律問答。

## 直接使用

- 建庫／向量化進度：
  https://dkqwnkplpgiauqknwbci.supabase.co/functions/v1/judicial-progress
- 判決搜尋（全文、判決號精確搜尋、中文語意搜尋）：
  https://dkqwnkplpgiauqknwbci.supabase.co/functions/v1/judicial-search-api
- RAG 檢索 API：
  `https://dkqwnkplpgiauqknwbci.supabase.co/functions/v1/judicial-rag-api?q=查詢內容&limit=8`

## 資料流程

司法院月資料 RAR → GitHub Actions 解壓／解析 → `legal_source_docs` → `legal_chunks` → PGroonga 全文索引 → BGE 中文向量 → RAG。

### 歷史匯入

- 319 個月份（2000/01～2026/07）
- GitHub Actions 平行處理
- Supabase 使用原子領取（`FOR UPDATE SKIP LOCKED`）避免重複搶同一月份
- 相同 `content_hash` 會跳過，不重建切片
- 失敗可續跑

### 判決搜尋

輸入 `109台抗1070`、`103台非222` 等格式時，會自動拆解年度／字別／號次，以資料庫欄位精確搜尋；一般文字使用 PGroonga 中文全文檢索。

### 中文語意索引

- 模型：`BAAI/bge-small-zh-v1.5`
- 向量維度：512
- `legal_chunks.embedding_zh`
- HNSW cosine index
- GitHub Actions 背景批次建立向量

### RAG 回答格式

後續生成模型接入後固定整理為：

`判決號 → 簡單案情 → 實務結論 → 一句話考點 → 常見陷阱`

生成模型必須只依檢索到的司法院裁判資料回答；來源不足時應明示資料不足，不得杜撰判決號或實務見解。

## 安全

- GitHub Worker 使用 GitHub OIDC 驗證，只接受 `korjiro40-design/jiajun-judicial-indexer` 的 `main` 分支。
- Supabase service role、司法院帳密、短效 Member Token 不存放在 Repository。
- 公開搜尋端點只提供公開裁判資料與唯讀 RPC。
