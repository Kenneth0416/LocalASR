function isLoopbackHostname(hostname) {
    const value = String(hostname || '').toLowerCase();
    return value === 'localhost' || value === '127.0.0.1' || value === '::1';
}

export function getAudioContextCtor(windowLike = globalThis.window) {
    return windowLike?.AudioContext || windowLike?.webkitAudioContext || null;
}

export function getLegacyGetUserMedia(navigatorLike = globalThis.navigator) {
    if (!navigatorLike) return null;
    return (
        navigatorLike.getUserMedia
        || navigatorLike.webkitGetUserMedia
        || navigatorLike.mozGetUserMedia
        || navigatorLike.msGetUserMedia
        || null
    );
}

export function describeRealtimeAudioSupport({
    navigatorLike = globalThis.navigator,
    windowLike = globalThis.window,
    locationLike = globalThis.location,
    isSecureContextValue = typeof globalThis.isSecureContext === 'boolean' ? globalThis.isSecureContext : false,
} = {}) {
    const audioContextCtor = getAudioContextCtor(windowLike);
    if (!audioContextCtor) {
        return {
            supported: false,
            message: '当前浏览器不支持 Web Audio，无法启动录音。',
            audioContextCtor: null,
        };
    }

    if (navigatorLike?.mediaDevices?.getUserMedia || getLegacyGetUserMedia(navigatorLike)) {
        return {
            supported: true,
            message: '',
            audioContextCtor,
        };
    }

    if (!isSecureContextValue && !isLoopbackHostname(locationLike?.hostname)) {
        return {
            supported: false,
            message: '当前页面不是安全上下文，浏览器已禁用麦克风。请改用 localhost 访问，或为内网部署启用 HTTPS。',
            audioContextCtor: null,
        };
    }

    return {
        supported: false,
        message: '当前浏览器不支持麦克风采集接口。请使用最新版 Chrome、Edge 或 Safari。',
        audioContextCtor: null,
    };
}

export async function requestRealtimeAudioStream(
    constraints,
    {
        navigatorLike = globalThis.navigator,
        windowLike = globalThis.window,
        locationLike = globalThis.location,
        isSecureContextValue = typeof globalThis.isSecureContext === 'boolean' ? globalThis.isSecureContext : false,
    } = {},
) {
    const support = describeRealtimeAudioSupport({
        navigatorLike,
        windowLike,
        locationLike,
        isSecureContextValue,
    });
    if (!support.supported) {
        throw new Error(support.message);
    }

    if (navigatorLike?.mediaDevices?.getUserMedia) {
        return navigatorLike.mediaDevices.getUserMedia(constraints);
    }

    const legacyGetUserMedia = getLegacyGetUserMedia(navigatorLike);
    if (!legacyGetUserMedia) {
        throw new Error('当前浏览器不支持麦克风采集接口。请使用最新版 Chrome、Edge 或 Safari。');
    }

    return new Promise((resolve, reject) => {
        legacyGetUserMedia.call(navigatorLike, constraints, resolve, reject);
    });
}
