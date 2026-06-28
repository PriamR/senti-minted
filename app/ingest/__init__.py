"""Discussion-source ingestion (YouTube, Reddit, ...).

Each ingester returns items normalized to the shape the measures pipeline
consumes: {text, timestamp, source, confidence, meta}. Topicality confidence is
1.0 for ground-truth sources and classifier-scored for wild sources.
"""
