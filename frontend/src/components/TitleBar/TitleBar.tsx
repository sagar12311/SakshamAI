/**
 * Saksham AI - Title Bar Component
 */

import { Minus, Square, X } from 'lucide-react';
import './TitleBar.css';

export default function TitleBar() {
    const handleMinimize = () => {
        window.saksham?.minimize();
    };

    const handleMaximize = () => {
        window.saksham?.maximize();
    };

    const handleClose = () => {
        window.saksham?.close();
    };

    return (
        <div className="titlebar">
            <div className="titlebar-drag" />

            <div className="titlebar-controls">
                <button
                    className="titlebar-btn minimize"
                    onClick={handleMinimize}
                    aria-label="Minimize"
                >
                    <Minus size={12} />
                </button>
                <button
                    className="titlebar-btn maximize"
                    onClick={handleMaximize}
                    aria-label="Maximize"
                >
                    <Square size={10} />
                </button>
                <button
                    className="titlebar-btn close"
                    onClick={handleClose}
                    aria-label="Close"
                >
                    <X size={12} />
                </button>
            </div>
        </div>
    );
}
