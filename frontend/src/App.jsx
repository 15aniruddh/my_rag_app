import { useCallback, useEffect, useRef, useState } from 'react'
import Markdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { askQuestion, clearLibrary, deleteDocument, getLibrary, uploadPdf, waitForDocument } from './api'

const MODEL = import.meta.env.VITE_MODEL_NAME ?? 'gemini-3.5-flash-lite'

function Sidebar({
  library, libraryError, onUpload, uploading, uploadNote,
  onDelete, onClear, busyDoc, clearing, theme, toggleTheme,
}) {
  const [dragOver, setDragOver] = useState(false)
  const fileRef = useRef(null)

  const handleFiles = (files) => {
    const pdf = [...files].find((f) => f.name.toLowerCase().endsWith('.pdf'))
    if (pdf) onUpload(pdf)
  }

  return (
    <aside className="sidebar">
      <div className="brand">
        <div className="brand-mark">📄</div>
        <div>
          <div className="brand-text">PDF Q&amp;A</div>
          <div className="brand-sub">ask your documents</div>
        </div>
      </div>

      <div>
        <div className="section-label">Library</div>
        {libraryError ? (
          <div className="toast error" style={{ marginTop: 10 }}>
            <span className="dot bad" />{libraryError}
          </div>
        ) : (
          <>
            <div className="stats">
              <div className="stat a"><b>{library.chunks}</b><span>Chunks</span></div>
              <div className="stat b"><b>{library.documents.length}</b><span>Documents</span></div>
            </div>
            {library.documents.length > 0 && (
              <>
                <div className="chips" style={{ marginTop: 12 }}>
                  {library.documents.map((d) => (
                    <span className="chip chip-doc" key={d} title={d}>
                      <span className="name">{d}</span>
                      <button
                        className="chip-x" aria-label={`Remove ${d}`}
                        disabled={busyDoc === d || clearing}
                        onClick={() => onDelete(d)}
                      >
                        {busyDoc === d ? '·' : '×'}
                      </button>
                    </span>
                  ))}
                </div>
                <button
                  className="btn" style={{ marginTop: 12 }}
                  disabled={clearing || busyDoc !== null}
                  onClick={onClear}
                >
                  {clearing ? 'Clearing…' : 'Clear library'}
                </button>
              </>
            )}
          </>
        )}
      </div>

      <div>
        <div className="section-label" style={{ marginBottom: 10 }}>Upload</div>
        <div
          className={`dropzone${dragOver ? ' over' : ''}`}
          onDragOver={(e) => { e.preventDefault(); setDragOver(true) }}
          onDragLeave={() => setDragOver(false)}
          onDrop={(e) => { e.preventDefault(); setDragOver(false); handleFiles(e.dataTransfer.files) }}
          onClick={() => fileRef.current?.click()}
        >
          {uploading
            ? <span className="typing"><i /><i /><i /></span>
            : <><strong>Drop a PDF</strong><br />or click to browse</>}
        </div>
        <input
          ref={fileRef} type="file" accept="application/pdf" hidden
          onChange={(e) => { handleFiles(e.target.files); e.target.value = '' }}
        />
        {uploadNote && (
          <div className={`toast${uploadNote.error ? ' error' : ''}`} style={{ marginTop: 10 }}>
            {uploadNote.text}
          </div>
        )}
      </div>

      <div className="spacer" />

      <div className="meta">
        <div><span className="dot ok" />Model · <code>{MODEL}</code></div>
        <div style={{ marginTop: 5 }}>Embeddings · local (fastembed)</div>
      </div>
      <button className="btn btn-ghost" onClick={toggleTheme}>
        {theme === 'dark' ? '☀  Light theme' : '☾  Dark theme'}
      </button>
    </aside>
  )
}

function Row({ side, label, children }) {
  return (
    <div className={`row ${side}`}>
      <div className={`avatar ${side}`}>{side === 'ask' ? 'You' : 'AI'}</div>
      <div className="bubble-wrap">
        <div className={`who ${side}`}>{label}</div>
        {children}
      </div>
    </div>
  )
}

export default function App() {
  const [library, setLibrary] = useState({ chunks: 0, documents: [] })
  const [libraryError, setLibraryError] = useState(null)
  const [history, setHistory] = useState([])
  const [question, setQuestion] = useState('')
  const [asking, setAsking] = useState(false)
  const [uploading, setUploading] = useState(false)
  const [uploadNote, setUploadNote] = useState(null)
  const [busyDoc, setBusyDoc] = useState(null)
  const [clearing, setClearing] = useState(false)
  const [theme, setTheme] = useState(
    () => localStorage.getItem('theme') ??
      (window.matchMedia?.('(prefers-color-scheme: light)').matches ? 'light' : 'dark'),
  )
  // Desktop: the sidebar collapses to give the chat the full width.
  // Mobile (<=900px): the same flag opens it as an overlay drawer.
  const [collapsed, setCollapsed] = useState(() => {
    const saved = localStorage.getItem('collapsed')
    if (saved !== null) return saved === '1'
    // Default collapsed on small screens, otherwise the drawer would be open
    // over the chat on first load.
    return window.matchMedia?.('(max-width: 900px)').matches ?? false
  })
  const [isMobile, setIsMobile] = useState(
    () => window.matchMedia?.('(max-width: 900px)').matches ?? false,
  )
  const endRef = useRef(null)

  useEffect(() => {
    document.documentElement.dataset.theme = theme
    localStorage.setItem('theme', theme)
  }, [theme])

  useEffect(() => { localStorage.setItem('collapsed', collapsed ? '1' : '0') }, [collapsed])

  // Track the breakpoint so the toggle can mean "collapse" or "open drawer".
  useEffect(() => {
    const mq = window.matchMedia('(max-width: 900px)')
    const onChange = (e) => setIsMobile(e.matches)
    mq.addEventListener('change', onChange)
    return () => mq.removeEventListener('change', onChange)
  }, [])

  // On phones the drawer starts closed and Escape shuts it.
  const drawerOpen = isMobile && !collapsed
  useEffect(() => {
    if (!drawerOpen) return
    const onKey = (e) => { if (e.key === 'Escape') setCollapsed(true) }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [drawerOpen])

  const refreshLibrary = useCallback(async () => {
    try {
      setLibrary(await getLibrary())
      setLibraryError(null)
    } catch (err) {
      setLibraryError(err.message)
    }
  }, [])

  useEffect(() => { refreshLibrary() }, [refreshLibrary])
  useEffect(() => { endRef.current?.scrollIntoView({ behavior: 'smooth' }) }, [history, asking])

  const handleUpload = async (file) => {
    setUploading(true)
    setUploadNote(null)
    try {
      const res = await uploadPdf(file)
      if (res.queued) {
        // Durable path: the chunk count does not exist yet, so poll instead.
        setUploadNote({ text: `Queued ${res.source} · indexing…` })
        const done = await waitForDocument(res.source)
        // Not "still indexing": by now the run has almost certainly failed, and
        // saying it will turn up shortly sends people away from the one place
        // that shows why it did not.
        setUploadNote(done
          ? { text: `Indexed ${res.source}` }
          : { text: `${res.source} did not finish indexing. The upload was accepted but the job did not complete — check the Inngest dashboard for the failed run.`, error: true })
      } else {
        setUploadNote({ text: `Indexed ${res.source} · ${res.ingested} chunks` })
      }
      await refreshLibrary()
    } catch (err) {
      setUploadNote({ text: err.message, error: true })
    } finally {
      setUploading(false)
    }
  }

  const handleDelete = async (source) => {
    setBusyDoc(source)
    setUploadNote(null)
    try {
      await deleteDocument(source)
      setUploadNote({ text: `Removed ${source}` })
      await refreshLibrary()
    } catch (err) {
      setUploadNote({ text: err.message, error: true })
    } finally {
      setBusyDoc(null)
    }
  }

  const handleClear = async () => {
    // Irreversible and removes everything, so confirm before firing.
    if (!window.confirm('Remove every indexed document? This cannot be undone.')) return
    setClearing(true)
    setUploadNote(null)
    try {
      await clearLibrary()
      setUploadNote({ text: 'Library cleared' })
      await refreshLibrary()
    } catch (err) {
      setUploadNote({ text: err.message, error: true })
    } finally {
      setClearing(false)
    }
  }

  const submit = async () => {
    const q = question.trim()
    if (!q || asking) return
    setQuestion('')
    setAsking(true)
    try {
      const res = await askQuestion(q)
      setHistory((h) => [...h, { question: q, ...res }])
    } catch (err) {
      setHistory((h) => [...h, { question: q, error: err.message }])
    } finally {
      setAsking(false)
    }
  }

  const sidebarHidden = isMobile ? !drawerOpen : collapsed

  return (
    <div className={`app${collapsed && !isMobile ? ' collapsed' : ''}${drawerOpen ? ' drawer-open' : ''}`}>
      <Sidebar
        library={library} libraryError={libraryError} onUpload={handleUpload}
        uploading={uploading} uploadNote={uploadNote}
        onDelete={handleDelete} onClear={handleClear}
        busyDoc={busyDoc} clearing={clearing}
        theme={theme} toggleTheme={() => setTheme((t) => (t === 'dark' ? 'light' : 'dark'))}
      />

      {drawerOpen && (
        <button className="backdrop" aria-label="Close menu" onClick={() => setCollapsed(true)} />
      )}

      <main className="main">
        <div className="topbar">
          <button
            className="icon-btn"
            aria-label={sidebarHidden ? 'Show sidebar' : 'Hide sidebar'}
            title={sidebarHidden ? 'Show sidebar' : 'Hide sidebar'}
            onClick={() => setCollapsed((c) => !c)}
          >
            {sidebarHidden ? '\u203a' : '\u2039'}
          </button>
          <div>
            <div className="topbar-title">PDF Q&amp;A</div>
            <div className="topbar-sub hide-sm">
              {library.documents.length
                ? `${library.documents.length} document${library.documents.length > 1 ? 's' : ''} · ${library.chunks} chunks`
                : 'no documents indexed'}
            </div>
          </div>
          <div className="topbar-right">
            {history.length > 0 && (
              <button className="pill" onClick={() => setHistory([])}>Clear chat</button>
            )}
            <span className="pill hide-sm">{MODEL}</span>
          </div>
        </div>

        <div className="messages">
          {history.length === 0 && !asking && (
            <div className="empty">
              <div className="empty-mark">💬</div>
              <h2>Ask about your documents</h2>
              <p>Upload a PDF, then ask a question about it.</p>
            </div>
          )}

          {history.map((turn, i) => (
            <div key={i} style={{ display: 'contents' }}>
              <Row side="ask" label="You">
                <div className="bubble ask">{turn.question}</div>
              </Row>
              <Row side="ans" label="Answer">
                {turn.error ? (
                  <div className="bubble ans error">{turn.error}</div>
                ) : (
                  <>
                    <div className="bubble ans">
                      {/* Answers come back as markdown; render it rather than
                          showing raw ** and * to the user. */}
                      <div className="md">
                        <Markdown remarkPlugins={[remarkGfm]}>{turn.answer}</Markdown>
                      </div>
                    </div>
                    {turn.sources?.length > 0 && (
                      <div className="chips">
                        {turn.sources.map((s) => <span className="chip" key={s}>📎 {s}</span>)}
                      </div>
                    )}
                  </>
                )}
              </Row>
            </div>
          ))}

          {asking && (
            <Row side="ans" label="Answer">
              <div className="bubble ans">
                <span className="typing"><i /><i /><i /></span>
              </div>
            </Row>
          )}
          <div ref={endRef} />
        </div>

        <div className="composer">
          <div className="composer-inner">
            <textarea
              rows={1} value={question} placeholder="Ask a question about your PDFs…"
              onChange={(e) => setQuestion(e.target.value)}
              // Enter sends, Shift+Enter makes a newline.
              onKeyDown={(e) => {
                if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); submit() }
              }}
            />
            <button className="send" onClick={submit} disabled={asking || !question.trim()}>
              {asking ? '…' : 'Ask'}
            </button>
          </div>
        </div>
      </main>
    </div>
  )
}
