/**
 * Saksham AI - Ambient Listening Indicator
 */

import { motion } from 'framer-motion';
import './AmbientIndicator.css';

interface AmbientIndicatorProps {
    isListening: boolean;
    hasPermission: boolean | null;
    onClick: () => void;
}

export default function AmbientIndicator({
    isListening,
    hasPermission,
    onClick
}: AmbientIndicatorProps) {
    return (
        <motion.button
            className={`ambient-indicator ${isListening ? 'active' : ''} ${hasPermission === false ? 'denied' : ''}`}
            onClick={onClick}
            whileHover={{ scale: 1.05 }}
            whileTap={{ scale: 0.95 }}
            title={isListening ? 'Ambient listening active' : 'Click to enable ambient listening'}
        >
            {isListening && (
                <span className="pulse-ring" />
            )}
            <span className="ambient-icon">
                {isListening ? '📶' : '📴'}
            </span>
            <span className="ambient-label">
                {isListening ? 'Listening' : 'Off'}
            </span>
        </motion.button>
    );
}
