/** Hook: makes an element a file drop target (spread `dropProps`, highlight via `dragging`). */
import { useRef, useState } from "react";

function hasFiles(e) {
  return Array.from(e.dataTransfer?.types ?? []).includes("Files");
}

export function useFileDrop(onFiles) {
  const [dragging, setDragging] = useState(false);
  // nested enter/leave counter prevents highlight flicker over children
  const depth = useRef(0);

  return {
    dragging,
    dropProps: {
      onDragEnter: (e) => {
        if (!hasFiles(e)) return;
        e.preventDefault();
        depth.current += 1;
        setDragging(true);
      },
      onDragOver: (e) => {
        if (!hasFiles(e)) return;
        e.preventDefault();
        if (e.dataTransfer) e.dataTransfer.dropEffect = "copy";
      },
      onDragLeave: (e) => {
        if (!hasFiles(e)) return;
        e.preventDefault();
        depth.current = Math.max(0, depth.current - 1);
        if (depth.current === 0) setDragging(false);
      },
      onDrop: (e) => {
        if (!hasFiles(e)) return;
        // nested target may have claimed the drop: clear highlight, don't double-add
        const claimed = e.isDefaultPrevented();
        e.preventDefault();
        depth.current = 0;
        setDragging(false);
        if (claimed) return;
        const files = Array.from(e.dataTransfer?.files ?? []);
        if (files.length) onFiles(files);
      },
    },
  };
}
