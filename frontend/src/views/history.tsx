import { useCallback, useEffect, useState } from "preact/hooks";
import { api } from "../api";
import backSvg from "../assets/icons/back.svg?raw";
import { ErrorBannerStack, useErrorStack } from "../components/error-banner";
import { Icon } from "../components/icon";
import { useNavigate } from "../components/router";
import type { HistoryFileInfo, MapData } from "../types";
import { HistoryItemView } from "./history/item";
import { HistoryListView } from "./history/list";

export function HistoryView() {
    const navigate = useNavigate();
    const [errors, errorStack] = useErrorStack();
    const [files, setFiles] = useState<HistoryFileInfo[]>([]);
    const [loading, setLoading] = useState(true);
    const [selectedIdx, setSelectedIdx] = useState<number | null>(null);
    const [selectedMap, setSelectedMap] = useState<MapData | null>(null);
    const [deleting, setDeleting] = useState(false);

    const selectedFile = selectedIdx !== null ? (files[selectedIdx] ?? null) : null;
    const selectedRecording = selectedFile?.recording === true;
    const hasRecording = files.some((f) => f.recording);

    // Sort sessions by date descending (newest first)
    const sortByDateDesc = (list: HistoryFileInfo[]) =>
        list.sort((a, b) => (b.session?.time ?? 0) - (a.session?.time ?? 0));

    // Load file list only (no full session data)
    useEffect(() => {
        setLoading(true);
        api.getHistoryList()
            .then((fileList) => setFiles(sortByDateDesc(fileList)))
            .catch((e: unknown) => {
                errorStack.push(e instanceof Error ? e.message : "Failed to load map data");
            })
            .finally(() => setLoading(false));
    }, []); // eslint-disable-line react-hooks/exhaustive-deps

    // Poll list + active recording session map (every 5s while recording)
    useEffect(() => {
        if (!hasRecording) return;
        const interval = setInterval(async () => {
            try {
                const fileList = await api.getHistoryList();
                setFiles(sortByDateDesc(fileList));

                // If the detail view shows the recording session, refresh its map
                if (selectedIdx !== null) {
                    const file = fileList[selectedIdx];
                    if (file?.recording) {
                        const maps = await api.getHistorySession(file.name);
                        if (maps.length > 0) setSelectedMap(maps[0]);
                    }
                }
            } catch {
                // Silently ignore poll errors
            }
        }, 5000);
        return () => clearInterval(interval);
    }, [hasRecording, selectedIdx]);

    // Fetch full session data when selecting a card
    const handleSelect = useCallback(
        async (idx: number) => {
            setSelectedIdx(idx);
            setSelectedMap(null);
            const file = files[idx];
            if (!file) return;
            try {
                const maps = await api.getHistorySession(file.name);
                if (maps.length > 0) setSelectedMap(maps[0]);
            } catch (e: unknown) {
                errorStack.push(e instanceof Error ? e.message : "Failed to load session");
            }
        },
        [files, errorStack],
    );

    const handleBack = useCallback(() => {
        if (selectedIdx !== null) {
            setSelectedIdx(null);
            setSelectedMap(null);
        } else {
            navigate("/");
        }
    }, [selectedIdx, navigate]);

    const handleDeleteSession = useCallback(
        (idx: number) => {
            const file = files[idx];
            if (!file) return;
            setDeleting(true);
            api.deleteHistorySession(file.name)
                .then(() => api.getHistoryList())
                .then((fileList) => {
                    setFiles(sortByDateDesc(fileList));
                    setSelectedIdx(null);
                    setSelectedMap(null);
                })
                .catch((e: unknown) => {
                    errorStack.push(e instanceof Error ? e.message : "Failed to delete");
                })
                .finally(() => setDeleting(false));
        },
        [files, errorStack],
    );

    const handleDeleteAll = useCallback(() => {
        setDeleting(true);
        api.deleteAllHistory()
            .then(() => {
                setFiles([]);
                setSelectedIdx(null);
                setSelectedMap(null);
            })
            .catch((e: unknown) => {
                errorStack.push(e instanceof Error ? e.message : "Failed to delete");
            })
            .finally(() => setDeleting(false));
    }, [errorStack]);

    const showDetail = selectedIdx !== null && selectedFile !== null;

    return (
        <>
            <div class="header">
                <button type="button" class="header-back-btn" onClick={handleBack} aria-label="Back">
                    <Icon svg={backSvg} />
                </button>
                <h1>{showDetail ? "Clean Map" : "Cleaning History"}</h1>
                <div class="header-right-spacer" />
            </div>

            <ErrorBannerStack errors={errors} />

            <div class="history-page">
                {loading && <div class="history-empty">Loading...</div>}

                {!loading && files.length === 0 && <div class="history-empty">No cleaning history yet</div>}

                {!loading && files.length > 0 && !showDetail && (
                    <HistoryListView
                        files={files}
                        hasRecording={hasRecording}
                        deleting={deleting}
                        onSelect={handleSelect}
                        onDeleteSession={handleDeleteSession}
                        onDeleteAll={handleDeleteAll}
                    />
                )}

                {!loading && showDetail && (
                    <HistoryItemView file={selectedFile} map={selectedMap} recording={selectedRecording} />
                )}
            </div>
        </>
    );
}
