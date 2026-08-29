# A-PAG AI Platform — Project Summary

## 1. What are we building?

We are building a **secure, scalable AI knowledge and data platform for A-PAG**.

The main goal is to allow employees to interact with organizational information using a **single AI interface**, instead of manually searching through documents, databases, reports, and other sources.

The platform will support two major types of questions:

1. **Knowledge/document questions** → answered using the organization's document knowledge base.
2. **Structured-data questions** → answered using PostgreSQL through **Natural Language to SQL (Text-to-SQL)**.

The system is designed with **data privacy, access control, maintainability, scalability, auditability, and correctness** as major requirements.

---

## 2. Main capabilities

The platform will have two primary AI paths.

```text
                         User
                           |
                     AI Chat Layer
                           |
                    Intent / Query Routing
                     /                 \
                    /                   \
                   ↓                     ↓
             Knowledge Base          PostgreSQL
                  RAG                 Text-to-SQL
                   |                     |
              Vector Search          SQL Generation
                   |                     |
              Relevant Chunks        SQL Validation
                   |                     |
                   └──────────┬──────────┘
                              ↓
                         AI Response
```

### Knowledge Base / RAG

Users can ask questions such as:

> "What was the objective of the Delhi project?"

The system searches the organization's documents, retrieves relevant content, and generates an answer based on that content.

### Text-to-SQL

Users can ask:

> "How much was spent on Project X last year?"

The system determines that this is a structured-data question, generates SQL, validates it, executes it against PostgreSQL, and converts the result into a natural-language answer.

---

## 3. Document ingestion pipeline

Documents will primarily start with **PDF support**.

The ingestion pipeline is:

```text
PDF Upload
    ↓
Quarantine
    ↓
Validation + Security Scan
    ↓
Raw Storage
    ↓
Queue
    ↓
Extraction
    ↓
Normalization
    ↓
Classification
    ↓
Supersede Check
    ↓
Chunking
    ↓
Embedding
    ↓
Indexing
    ↓
Knowledge Base
```

The pipeline is asynchronous so that uploading a large number of documents does not block the API.

---

## 4. Upload and validation

When a user uploads a PDF, it first goes into **temporary/quarantine storage**.

The system validates:

- File type
- PDF validity
- File size
- SHA-256 checksum
- Malware/virus scan

The document is not moved to permanent raw storage until validation succeeds.

```text
Upload
  ↓
Quarantine
  ↓
Validation
 ├── Failed → Delete + Reject
 │
 └── Passed → Raw Storage
```

This prevents potentially malicious or invalid files from entering the main document pipeline.

---

## 5. Extraction

After validation, a background worker processes the PDF.

The system first attempts **native PDF text extraction**.

If the PDF contains little or no usable text, an **OCR fallback** is triggered.

```text
PDF
 ↓
Native Extraction
 ↓
Text Quality Check
 ↓
Enough text?
 ├── YES → Continue
 │
 └── NO → OCR
```

The extraction layer is designed using an abstraction:

```text
TextExtractor
     |
 ┌───┴────────┐
 ↓            ↓
PDF Extractor OCR Extractor
```

This makes it possible to replace extraction/OCR implementations later without changing the main extraction service.

The extracted information is stored as an extraction artifact.

---

## 6. Normalization

Raw extracted text is not immediately suitable for RAG.

The normalization stage cleans and structures it.

It handles:

- Text cleanup
- Whitespace/encoding normalization
- Header/footer cleanup
- Section detection
- Heading detection
- Table extraction
- List detection
- Entity extraction
- Language detection
- Page mapping
- Quality checks

The result is a structured normalized document.

```text
Extraction
    ↓
Text Cleaning
    ↓
Structure Detection
    ↓
Table Extraction
    ↓
Entity Extraction
    ↓
Language Detection
    ↓
Quality Checks
    ↓
Normalized Document
```

---

## 7. Classification hard gate

Classification is a **mandatory gate** before the document can enter the knowledge base.

The current simple approach is based on whether the document is:

- **Public**
- **Confidential**
- and the broader architecture can support the required organizational classification levels.

The important rule is:

> A document cannot continue to indexing until its classification is confirmed.

The classification information is also associated with the document/chunks so that retrieval can enforce permissions.

---

## 8. Permissions and security

The system is designed around **permission-aware retrieval**.

A user's permissions are determined before retrieval.

The resulting allow-list is passed directly into the search operation.

```text
User
 ↓
Identity / Permissions
 ↓
Allowed Documents
 ↓
Hard Search Filter
 ↓
Qdrant / Search
```

This is important because restricted content should not first be retrieved and then removed afterward.

The permission information is associated with indexed chunks/documents so the search layer can apply the filter during retrieval.

---

## 9. Supersede and document versions

When a new document replaces an older document, the system needs to detect that relationship.

The old version should be marked as **superseded** and removed from active retrieval indexes.

However, the historical document can remain in controlled storage/database records for audit purposes.

This prevents the AI from answering using both:

```text
Current policy
+
Old policy
```

at the same time.

---

## 10. Chunking and embeddings

After classification and lifecycle checks, documents are divided into smaller meaningful **chunks**.

Each chunk retains metadata such as:

- Document ID
- Chunk ID
- Page
- Section
- Classification
- Permission information
- Version information

The chunks are converted into embeddings.

The embeddings are stored in the **vector database**, currently planned around Qdrant.

---

## 11. Why Qdrant?

Qdrant is used for **semantic/vector search**.

For example:

> "How much did the Delhi project spending increase?"

can find a document chunk discussing:

> "Delhi project expenditure increased by 12%."

even when the exact words don't match.

Qdrant can also store metadata/payload alongside vectors, allowing permission-related filtering during retrieval.

---

## 12. OpenSearch

OpenSearch is **not mandatory for the first MVP**.

Its purpose is keyword/full-text search.

For example:

```text
Project ID: A-PAG-1024
```

Exact identifiers and names can sometimes benefit from keyword search more than semantic search.

The future architecture can therefore support:

```text
             Search
            /      \
       Qdrant    OpenSearch
      Semantic    Keyword
        Search     Search
            \      /
             Fusion
```

For the initial implementation, the team can evaluate whether Qdrant + PostgreSQL search is sufficient before adding OpenSearch.

This keeps the first version simpler and more maintainable.

---

## 13. Retrieval pipeline

For a document-based question:

```text
User Question
      ↓
Query Processing
      ↓
Permission Allow-list
      ↓
Vector / Keyword Search
      ↓
Permission-filtered Results
      ↓
Fusion / Deduplication
      ↓
Reranking
      ↓
Freshness / Supersede Checks
      ↓
Relevant Context
      ↓
LLM
      ↓
Answer Critic
      ↓
Final Answer
```

The answer critic is intended to check whether the generated answer is actually supported by the retrieved evidence.

If the answer cannot be reliably supported, the system should be able to refuse rather than confidently invent an answer.

---

## 14. Text-to-SQL

The second major capability is **Natural Language to SQL**.

The user doesn't need to know SQL.

Example:

```text
User:
"How many projects were completed in 2025?"
```

The system:

```text
Question
   ↓
Intent / Query Routing
   ↓
Relevant Schema / Semantic Context
   ↓
Text-to-SQL Model
   ↓
Generated SQL
   ↓
SQL Validator
   ↓
Read-only PostgreSQL
   ↓
Result
   ↓
Natural-language Answer
```

The model should not receive the entire database unnecessarily.

Only the relevant schema/context should be provided.

The generated SQL must be validated before execution.

The database should be protected from generated writes/deletes.

---

## 15. Text-to-SQL model

The project is currently evaluating **Sarvam and open-source Text-to-SQL models**.

Sarvam is being evaluated because the project contains confidential organizational information.

The current discussion with Sarvam is focused on:

- India-based inference
- Zero retention
- No training/fine-tuning on API data
- Security certifications
- Data-processing terms
- Sub-processors
- Commercial limits
- Context limits

No production confidential data should be sent until these requirements are formally approved.

---

## 16. Sarvam + confidential data

The platform may use Sarvam for inference if the organization's policy allows data to leave its infrastructure under the required conditions.

The intended model is:

```text
A-PAG PostgreSQL
        |
        | Relevant schema/context
        ↓
     A-PAG AI Layer
        |
        ↓
    Sarvam API
        |
        ↓
     AI Response
```

For RAG:

```text
A-PAG Vector DB
       ↓
Permission-filtered relevant chunks
       ↓
A-PAG AI Layer
       ↓
Sarvam API
```

The complete PostgreSQL database or complete Vector DB should **never be sent to the model**.

---

## 17. Testing Sarvam safely

Before production testing, use **sanitized data**.

For PostgreSQL:

```text
Production Database
       ↓
Sanitization
       ↓
Test Database
       ↓
Text-to-SQL Testing
```

For documents:

```text
Real Documents
       ↓
Sanitization / Synthetic Documents
       ↓
Test Vector DB
       ↓
RAG Testing
```

The test harness should evaluate things such as:

- SQL correctness
- Execution accuracy
- RAG answer accuracy
- Permission violations
- Refusal behavior
- Latency
- Token usage
- Cost
- Failure cases

A previous Text-to-SQL test harness already achieved:

```text
12 questions
10 correct
1 wrong
1 refused
0 validator rejections
0 execution errors
```

This type of evaluation should be expanded using real representative questions after sanitized test data is available.

---

## 18. Government documents / external sources

The system can also potentially ingest documents from approved government websites.

The flow would be:

```text
Approved Government Source
        ↓
Download
        ↓
Quarantine
        ↓
Validation + Virus Scan
        ↓
Existing Ingestion Pipeline
```

The system should maintain source information such as:

- Source URL
- Organization
- Publication date
- Download date
- License/usage conditions
- Checksum
- Document version

External documents should go through the **same ingestion pipeline** as internally uploaded documents rather than having a separate processing system.

---

## 19. Scalable processing architecture

The ingestion system is designed around asynchronous workers.

```text
FastAPI
   ↓
Queue
   ↓
Worker Pool
 ┌────┬────┬────┐
 W1   W2   W3 ... WN
 └────┴────┴────┘
       ↓
Processing Services
```

This allows multiple documents to be processed simultaneously.

For example:

```text
5,000 documents
      ↓
Queue
      ↓
Worker Pool
      ↓
Automatic scaling
```

KEDA can later be used to scale workers based on queue depth.

The system also includes:

- Retry
- Exponential backoff
- Idempotency
- Dead-letter queue
- Failure tracking
- Monitoring

The exact worker count should be determined through load testing rather than hardcoding a fixed number.

---

## 20. Storage architecture

The major storage responsibilities are separated.

### PostgreSQL

Used for structured application data such as:

- Document metadata
- Processing status
- Classification
- Permissions
- Jobs
- Audit information
- Structured business data used by Text-to-SQL

### MinIO

Used for object storage such as:

```text
quarantine/
raw/
extracted/
normalized/
```

Examples:

```text
/extracted/{document_id}/extraction.json
/normalized/{document_id}/normalized.json
```

### Qdrant

Used for:

- Embeddings
- Chunks
- Retrieval metadata
- Vector search

This separation keeps each storage system focused on its purpose.

---

## 21. Application architecture

The backend is being designed around **FastAPI**.

The code is organized using a service-oriented structure rather than putting all logic inside API routes.

The design pattern is:

```text
Controller / Worker
        ↓
      Service
        ↓
    Interface
        ↓
 Implementation
        ↓
Infrastructure
```

For example:

```text
ExtractionService
        ↓
TextExtractor
        ↓
PdfTextExtractor / OcrExtractor
```

and:

```text
UploadService
        ↓
ObjectStorage
        ↓
MinIOStorage
```

This makes the system easier to maintain and allows implementations to be replaced without rewriting the business logic.

---

## 22. Class and sequence design

The project is being documented at LLD level using **UML Design Class Diagrams and Sequence Diagrams**.

Current class diagrams cover areas such as:

- Upload & validation
- Extraction & normalization

The class design includes:

- Classes
- Data members
- Methods
- Interfaces
- Implementations
- Repository abstractions
- Storage abstractions
- Worker/service separation

Sequence diagrams show:

- Upload and validation
- Quarantine → raw storage
- Extraction
- OCR fallback
- Normalization
- Worker concurrency
- Retry/backoff
- DLQ
- Scaling

This documentation is intended to make the project understandable to another developer after the current developer leaves.

---

## 23. Development environment

The project is planned to use:

**Python + uv**

rather than Conda or multiple environment systems.

Repository structure will use:

```text
pyproject.toml
uv.lock
.python-version
.env.example
```

`uv.lock` provides reproducible dependency versions for developers and CI/CD.

The goal is that a new developer can clone the repository and get started with a small number of commands rather than manually recreating the environment.

---

## 24. Maintainability and scalability goals

A major project requirement is:

> **The system should be maintainable by another developer and scalable for approximately 50 organizational users and potentially other organizations in the future.**

Therefore, the design emphasizes:

- Modular services
- Interfaces
- Dependency separation
- Stateless API services
- Background workers
- Queue-based processing
- Database connection pooling
- Idempotent jobs
- Retry handling
- DLQ
- Centralized configuration
- Reproducible environments
- Automated testing
- Monitoring
- Auditability
- Clear documentation

The goal is not simply to make a prototype work.

The goal is to make the system **replaceable, testable, maintainable, and deployable at organizational scale**.

---

## 25. Security principles

The major security principles are:

### Data minimization

Only send the minimum required context to an external AI provider.

### Quarantine

Uploaded files are isolated before validation.

### Malware scanning

Files are scanned before entering the raw document store.

### Classification gate

Documents cannot enter the searchable knowledge base without classification.

### Permission-aware retrieval

Permissions are applied as part of the search rather than filtering results afterward.

### Auditability

Important document and processing actions are recorded.

### Data sovereignty

External AI providers are evaluated against organizational data-processing requirements.

### Read-only Text-to-SQL

Generated SQL should be validated and executed against a controlled/read-only database path.

---

## 26. Overall system

The complete vision is:

```text
                         A-PAG AI PLATFORM
                                |
                         ┌──────┴──────┐
                         ↓             ↓
                    Knowledge       Data
                    Questions      Questions
                         |             |
                         ↓             ↓
                       RAG        Text-to-SQL
                         |             |
                    Vector DB      PostgreSQL
                         |             |
                         └──────┬──────┘
                                ↓
                          AI Chat Layer
                                |
                         Answer Validation
                                |
                         Final Response
```

Behind this sits the document pipeline:

```text
PDF
 ↓
Quarantine
 ↓
Validation + Virus Scan
 ↓
Raw Storage
 ↓
Queue
 ↓
Extraction
 ↓
OCR if required
 ↓
Normalization
 ↓
Classification
 ↓
Supersede Check
 ↓
Chunking
 ↓
Embedding
 ↓
Qdrant / Search
```

---

## 27. End goal

The final platform should allow an employee to simply ask:

> **"What do I need to know about this project?"**

or:

> **"How much was spent on this project last year?"**

without needing to know whether the answer lives inside:

- A PDF
- A report
- A structured PostgreSQL table
- A document chunk
- Multiple sources

The system should determine the appropriate path, retrieve only information the user is authorized to access, generate an evidence-based answer, and avoid making unsupported claims.

In short:

> **We are building a secure enterprise AI platform that combines a permission-aware document knowledge base/RAG system with a controlled PostgreSQL Text-to-SQL system, supported by an asynchronous, scalable ingestion pipeline and a maintainable FastAPI-based backend.**
