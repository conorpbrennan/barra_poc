import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

// Prose in the ~46rem reading column (et-book serif), matching the static docs. Used for all the
// streamed LLM commentary (/analysis, /ask, /whatchanged/analysis).
export function Markdown({ text }: { text: string }) {
  return (
    <div className="reading">
      <ReactMarkdown remarkPlugins={[remarkGfm]}>{text}</ReactMarkdown>
    </div>
  );
}
