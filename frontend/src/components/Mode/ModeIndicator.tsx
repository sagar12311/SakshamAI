/**
 * Saksham AI - Mode Indicator Component
 */

import { motion } from 'framer-motion';
import { Shield, Zap, Bot, Eye } from 'lucide-react';
import './ModeIndicator.css';

type Mode = 'assist' | 'execute' | 'autonomous' | 'shadow';

interface ModeIndicatorProps {
    mode: Mode;
    onModeChange: (mode: Mode) => void;
}

const modeConfig = {
    assist: {
        icon: Shield,
        label: 'Assist',
        description: 'Suggests, waits for approval',
        color: '#10b981',
    },
    execute: {
        icon: Zap,
        label: 'Execute',
        description: 'Brief confirmation, then acts',
        color: '#f59e0b',
    },
    autonomous: {
        icon: Bot,
        label: 'Autonomous',
        description: 'Acts within boundaries',
        color: '#6366f1',
    },
    shadow: {
        icon: Eye,
        label: 'Shadow',
        description: 'Observes and learns',
        color: '#8b5cf6',
    },
};

export default function ModeIndicator({ mode, onModeChange }: ModeIndicatorProps) {
    const config = modeConfig[mode];
    const Icon = config.icon;

    const modes: Mode[] = ['assist', 'execute', 'autonomous', 'shadow'];
    const currentIndex = modes.indexOf(mode);

    const handleCycle = () => {
        const nextIndex = (currentIndex + 1) % modes.length;
        onModeChange(modes[nextIndex]);
    };

    return (
        <motion.button
            className="mode-indicator"
            onClick={handleCycle}
            style={{ '--mode-color': config.color } as any}
            whileHover={{ scale: 1.02 }}
            whileTap={{ scale: 0.98 }}
        >
            <span className="mode-icon">
                <Icon size={16} />
            </span>
            <span className="mode-label">{config.label}</span>
        </motion.button>
    );
}
