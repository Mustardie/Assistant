/**
 * Shared helpers for the ExtendScript host.
 *
 * Three concerns live here and nowhere else:
 *
 *  1. Time. Premiere mixes Time objects, tick strings and timecode depending on
 *     which API you touch. Everything crossing the bridge is float seconds, and
 *     these helpers are the only place that conversion happens.
 *
 *  2. Errors. Every failure becomes a structured object with a code and a hint,
 *     because the consumer is a model that has to decide what to do next.
 *
 *  3. Feature detection. Premiere's scripting surface differs between builds
 *     and the QE DOM is undocumented. `has()` and `qeDom()` make "is this
 *     available" a question we answer by asking, not by assuming.
 */

var NovaUtil = (function () {
    var TICKS_PER_SECOND = 254016000000;

    // ------------------------------------------------------------------
    // Errors
    // ------------------------------------------------------------------

    function fail(message, options) {
        var error = { code: 'host_error', message: String(message) };
        if (options) {
            if (options.code) { error.code = options.code; }
            if (options.hint) { error.hint = options.hint; }
            if (options.detail) { error.detail = options.detail; }
            if (options.alternative) { error.alternative = options.alternative; }
        }
        var wrapped = new Error(String(message));
        wrapped.novaError = error;
        return wrapped;
    }

    function unsupported(message, alternative, detail) {
        return fail(message, {
            code: 'unsupported',
            alternative: alternative || '',
            detail: detail
        });
    }

    // ------------------------------------------------------------------
    // Time
    // ------------------------------------------------------------------

    /** Build a Premiere Time object from float seconds. */
    function timeFromSeconds(seconds) {
        var time = new Time();
        // Prefer .seconds (simple, exact enough for frame-aligned edits); fall
        // back to ticks on builds where the setter is missing.
        try {
            time.seconds = Number(seconds);
            return time;
        } catch (e) {
            time.ticks = String(Math.round(Number(seconds) * TICKS_PER_SECOND));
            return time;
        }
    }

    /** Float seconds from a Premiere Time object (or anything time-like). */
    function secondsOf(time) {
        if (time === null || time === undefined) { return 0; }
        if (typeof time === 'number') { return time; }
        try {
            if (time.seconds !== undefined && time.seconds !== null) {
                return Number(time.seconds);
            }
        } catch (e) { /* fall through to ticks */ }
        try {
            return Number(time.ticks) / TICKS_PER_SECOND;
        } catch (e2) {
            return 0;
        }
    }

    function ticksOf(seconds) {
        return String(Math.round(Number(seconds) * TICKS_PER_SECOND));
    }

    /** Seconds -> "HH:MM:SS:FF", which is what the QE DOM wants. */
    function timecode(seconds, fps) {
        var rate = fps || 30;
        var frames = Math.round(Number(seconds) * rate);
        var perSecond = Math.round(rate);
        var ff = frames % perSecond;
        var total = Math.floor(frames / perSecond);
        function pad(n) { return (n < 10 ? '0' : '') + n; }
        return pad(Math.floor(total / 3600)) + ':' +
               pad(Math.floor(total / 60) % 60) + ':' +
               pad(total % 60) + ':' + pad(ff);
    }

    function sequenceFps(sequence) {
        var seq = sequence || app.project.activeSequence;
        if (!seq) { return 30; }
        try {
            var settings = seq.getSettings();
            if (settings && settings.videoFrameRate) {
                var ticks = Number(settings.videoFrameRate.ticks);
                if (ticks > 0) { return TICKS_PER_SECOND / ticks; }
            }
        } catch (e) { /* fall back below */ }
        try {
            // timebase is ticks-per-frame as a string on most builds.
            var timebase = Number(seq.timebase);
            if (timebase > 0) { return TICKS_PER_SECOND / timebase; }
        } catch (e2) { /* give up and assume 30 */ }
        return 30;
    }

    function frameSize(sequence) {
        var seq = sequence || app.project.activeSequence;
        var size = { width: 1920, height: 1080 };
        if (!seq) { return size; }
        try {
            var settings = seq.getSettings();
            if (settings) {
                if (settings.videoFrameWidth) { size.width = Number(settings.videoFrameWidth); }
                if (settings.videoFrameHeight) { size.height = Number(settings.videoFrameHeight); }
            }
        } catch (e) {
            try {
                size.width = Number(seq.frameSizeHorizontal) || size.width;
                size.height = Number(seq.frameSizeVertical) || size.height;
            } catch (e2) { /* defaults stand */ }
        }
        return size;
    }

    // ------------------------------------------------------------------
    // Feature detection
    // ------------------------------------------------------------------

    function has(object, member) {
        if (!object) { return false; }
        try {
            return typeof object[member] === 'function' ||
                   object[member] !== undefined;
        } catch (e) {
            return false;
        }
    }

    var _qe = null;
    var _qeTried = false;

    /**
     * The QE DOM, or null.
     *
     * QE is Adobe's internal, undocumented and unsupported scripting surface.
     * It is also the ONLY way to razor a clip, apply a transition or enumerate
     * installed effects. We use it where there is no alternative, always behind
     * a feature check, and caps.probe reports whether it was available so the
     * planning side never assumes it.
     */
    function qeDom() {
        if (_qeTried) { return _qe; }
        _qeTried = true;
        try {
            if (typeof qe === 'undefined' || !qe) {
                app.enableQE();
            }
            _qe = (typeof qe !== 'undefined') ? qe : null;
        } catch (e) {
            _qe = null;
        }
        return _qe;
    }

    function requireQE(what) {
        var dom = qeDom();
        if (!dom) {
            throw unsupported(
                what + ' needs the QE DOM, which this Premiere build did not expose',
                'restart Premiere, or use an approach that avoids QE (see caps.probe)'
            );
        }
        return dom;
    }

    function activeSequence() {
        var sequence = app.project.activeSequence;
        if (!sequence) {
            throw fail('No active sequence', {
                hint: 'Open a sequence in Premiere, or use sequence.activate'
            });
        }
        return sequence;
    }

    function qeSequence() {
        var dom = requireQE('this operation');
        var sequence = dom.project.getActiveSequence();
        if (!sequence) {
            throw fail('QE could not see an active sequence', {
                hint: 'Click the timeline once to give it focus, then retry'
            });
        }
        return sequence;
    }

    // ------------------------------------------------------------------
    // Misc
    // ------------------------------------------------------------------

    function isArray(value) {
        return Object.prototype.toString.apply(value) === '[object Array]';
    }

    function defined(value, fallback) {
        return (value === undefined || value === null) ? fallback : value;
    }

    /** Case/spacing-insensitive name match, for effect and parameter lookup. */
    function normalise(name) {
        return String(name === undefined || name === null ? '' : name)
            .toLowerCase().replace(/[\s_\-]/g, '');
    }

    function nameMatches(candidate, wanted) {
        return normalise(candidate) === normalise(wanted);
    }

    return {
        TICKS_PER_SECOND: TICKS_PER_SECOND,
        fail: fail,
        unsupported: unsupported,
        timeFromSeconds: timeFromSeconds,
        secondsOf: secondsOf,
        ticksOf: ticksOf,
        timecode: timecode,
        sequenceFps: sequenceFps,
        frameSize: frameSize,
        has: has,
        qeDom: qeDom,
        requireQE: requireQE,
        qeSequence: qeSequence,
        activeSequence: activeSequence,
        isArray: isArray,
        defined: defined,
        normalise: normalise,
        nameMatches: nameMatches
    };
}());
