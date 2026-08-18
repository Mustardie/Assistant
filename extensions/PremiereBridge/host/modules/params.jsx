/**
 * Generic property and keyframe engine.
 *
 * This is the module that makes the whole design work. Instead of one host
 * function per creative intention, there is one path that can address ANY
 * parameter of ANY component on ANY clip -- Motion > Scale, Opacity > Opacity,
 * Volume > Level, Lumetri Color > Exposure, Gaussian Blur > Blurriness, a
 * MOGRT's text field, Time Remapping > Speed -- and read it, write it, or
 * animate it.
 *
 * The two subtleties worth knowing:
 *
 * 1. Parameter time is NOT sequence time. Premiere expresses a clip's keyframe
 *    times relative to the source media's in-point, so a keyframe "1 second
 *    into the clip" lives at (inPoint + 1). Every conversion goes through
 *    paramTime() so this is handled in exactly one place; getting it wrong
 *    puts keyframes at plausible-looking but wrong times, which is very hard
 *    to spot on a timeline.
 *
 * 2. A parameter must be switched to time-varying before it will accept
 *    keyframes, and switching it AFTER writing values silently discards them.
 */

var NovaParams = (function () {
    var U = NovaUtil;
    var T = NovaTimeline;

    /**
     * Interpolation mode constants.
     *
     * Premiere's KFInterpMode values are not documented in the public
     * scripting reference. These are the best-known mapping; caps.probe
     * measures which of them the installed build actually accepts and reports
     * the result, so nothing downstream has to trust this table blindly.
     *
     * Exact curve shapes never depend on this: engine-side baking produces
     * curves from VALUES, not from interpolation modes.
     */
    var INTERP = {
        linear: 0,
        hold: 1,
        ease_in: 2,
        ease_out: 3,
        ease_both: 4,
        bezier: 5,
        auto_bezier: 6,
        continuous_bezier: 7
    };

    // ------------------------------------------------------------------
    // Lookup
    // ------------------------------------------------------------------

    function findComponent(clip, name, occurrence) {
        var wanted = name ? String(name) : 'Motion';
        var seen = 0;
        var target = (occurrence === undefined || occurrence === null)
            ? 0 : Number(occurrence);
        var available = [];

        for (var i = 0; i < clip.components.numItems; i++) {
            var component = clip.components[i];
            var display = String(component.displayName);
            available[available.length] = display;
            if (U.nameMatches(display, wanted) ||
                U.nameMatches(component.matchName, wanted)) {
                if (seen === target) { return component; }
                seen++;
            }
        }

        throw U.fail(
            'Clip "' + clip.name + '" has no component named "' + wanted + '"' +
            (target ? ' at occurrence ' + target : ''),
            {
                hint: 'Available on this clip: ' + available.join(', ') +
                      '. Apply the effect first with effect.apply, or call '
                      + 'caps.params to see the real names.',
                detail: { available: available }
            }
        );
    }

    function findParam(component, name) {
        var available = [];
        for (var i = 0; i < component.properties.numItems; i++) {
            var param = component.properties[i];
            var display = String(param.displayName);
            available[available.length] = display;
            if (U.nameMatches(display, name)) { return param; }
        }
        throw U.fail(
            '"' + component.displayName + '" has no parameter named "' + name + '"',
            {
                hint: 'Parameters on this component: ' + available.join(', '),
                detail: { available: available }
            }
        );
    }

    function target(params) {
        var match = T.resolveOne(params.clip);
        var component = findComponent(match.clip, params.component, params.index);
        var param = findParam(component, params.property);
        return {
            match: match,
            clip: match.clip,
            component: component,
            param: param
        };
    }

    // ------------------------------------------------------------------
    // Time
    // ------------------------------------------------------------------

    /**
     * Convert a request time into parameter (source) time.
     *
     * relative_to 'clip'     -> t seconds after the clip's visible start
     * relative_to 'sequence' -> absolute timeline position
     */
    function paramTime(clip, seconds, relativeTo) {
        var inPoint = U.secondsOf(clip.inPoint);
        if (relativeTo === 'sequence') {
            return inPoint + (Number(seconds) - U.secondsOf(clip.start));
        }
        return inPoint + Number(seconds);
    }

    function fromParamTime(clip, seconds, relativeTo) {
        var inPoint = U.secondsOf(clip.inPoint);
        if (relativeTo === 'sequence') {
            return (Number(seconds) - inPoint) + U.secondsOf(clip.start);
        }
        return Number(seconds) - inPoint;
    }

    // ------------------------------------------------------------------
    // Values
    // ------------------------------------------------------------------

    function readValue(param) {
        try {
            return param.getValue();
        } catch (e) {
            throw U.fail('Could not read "' + param.displayName + '": ' + e);
        }
    }

    function writeValue(param, value) {
        try {
            param.setValue(coerce(param, value), true);
            return true;
        } catch (e) {
            throw U.fail(
                'Could not set "' + param.displayName + '" to ' +
                JSON.stringify(value) + ': ' + e,
                {
                    hint: 'Check the expected shape with caps.params -- some '
                        + 'parameters need a list (e.g. Position is [x, y]) and '
                        + 'some are read-only.'
                }
            );
        }
    }

    /**
     * Match the value's shape to what the parameter currently holds.
     *
     * Premiere throws unhelpfully when handed a scalar for a 2-vector
     * parameter (a very common model mistake: `Position: 0.5`). Widening a
     * scalar to the existing arity is friendlier and unambiguous.
     */
    function coerce(param, value) {
        if (!U.isArray(value)) { return value; }
        var current;
        try { current = param.getValue(); } catch (e) { current = null; }
        if (U.isArray(current) && current.length !== value.length) {
            if (value.length === 1) {
                var widened = [];
                for (var i = 0; i < current.length; i++) { widened[i] = value[0]; }
                return widened;
            }
        }
        return value;
    }

    function ensureTimeVarying(param) {
        if (!param.areKeyframesSupported()) {
            throw U.unsupported(
                'Parameter "' + param.displayName + '" cannot be keyframed',
                'set it statically with property.set, or check caps.params for '
                    + 'which parameters on this clip support keyframes'
            );
        }
        try {
            if (!param.isTimeVarying()) { param.setTimeVarying(true); }
        } catch (e) {
            throw U.fail('Could not enable keyframing on "' +
                         param.displayName + '": ' + e);
        }
    }

    // ------------------------------------------------------------------
    // Operations
    // ------------------------------------------------------------------

    function get(params) {
        var found = target(params);
        var param = found.param;
        var out = {
            clip: found.clip.name,
            component: String(found.component.displayName),
            property: String(param.displayName),
            keyframable: safeCall(param, 'areKeyframesSupported', false),
            animated: safeCall(param, 'isTimeVarying', false)
        };

        if (params.at !== undefined && out.animated) {
            var at = paramTime(found.clip, params.at,
                               params.relative_to || 'clip');
            try {
                out.value = param.getValueAtKey(U.timeFromSeconds(at));
                out.at = Number(params.at);
            } catch (e) {
                out.value = readValue(param);
                out.note = 'No keyframe at that time; returned the static value.';
            }
        } else {
            out.value = readValue(param);
        }

        if (out.animated) {
            out.keyframes = keyTimes(param, found.clip,
                                     params.relative_to || 'clip').length;
        }
        return out;
    }

    function safeCall(object, method, fallback) {
        try {
            return (typeof object[method] === 'function') ? object[method]() : fallback;
        } catch (e) {
            return fallback;
        }
    }

    function set(params) {
        var matches = T.resolve(params.clip);
        var results = [];
        for (var i = 0; i < matches.length; i++) {
            var clip = matches[i].clip;
            var component = findComponent(clip, params.component, params.index);
            var param = findParam(component, params.property);

            if (params.at !== undefined) {
                ensureTimeVarying(param);
                var at = paramTime(clip, params.at, params.relative_to || 'clip');
                var time = U.timeFromSeconds(at);
                param.addKey(time);
                param.setValueAtKey(time, coerce(param, params.value), true);
                results[results.length] = {
                    clip: String(clip.name), keyframe_at: Number(params.at)
                };
            } else {
                writeValue(param, params.value);
                results[results.length] = {
                    clip: String(clip.name), value: params.value
                };
            }
        }
        return { updated: results.length, results: results };
    }

    function reset(params) {
        var matches = T.resolve(params.clip);
        var cleared = 0;
        for (var i = 0; i < matches.length; i++) {
            var component = findComponent(matches[i].clip, params.component,
                                          params.index);
            var param = findParam(component, params.property);
            try {
                if (safeCall(param, 'isTimeVarying', false)) {
                    param.setTimeVarying(false);
                }
                cleared++;
            } catch (e) { /* nothing to clear */ }
        }
        return {
            cleared: cleared,
            note: 'Keyframes removed. Premiere exposes no "restore default '
                + 'value" call, so the parameter keeps its current static value '
                + '-- set it explicitly with property.set if you need the default.'
        };
    }

    function keyTimes(param, clip, relativeTo) {
        var out = [];
        try {
            var keys = param.getKeys();
            for (var i = 0; i < keys.length; i++) {
                out[out.length] = {
                    param_time: U.secondsOf(keys[i]),
                    time: fromParamTime(clip, U.secondsOf(keys[i]), relativeTo),
                    raw: keys[i]
                };
            }
        } catch (e) { /* not animated, or keys unreadable */ }
        return out;
    }

    function listKeys(params) {
        var found = target(params);
        var relativeTo = params.relative_to || 'clip';
        var keys = keyTimes(found.param, found.clip, relativeTo);
        var out = [];
        for (var i = 0; i < keys.length; i++) {
            var entry = { time: keys[i].time };
            try { entry.value = found.param.getValueAtKey(keys[i].raw); }
            catch (e) { /* value unreadable */ }
            try {
                var mode = found.param.getInterpolationTypeAtKey(keys[i].raw);
                entry.interp = interpName(mode);
                entry.interp_code = mode;
            } catch (e2) { /* not all builds expose the getter */ }
            out[out.length] = entry;
        }
        return {
            clip: String(found.clip.name),
            component: String(found.component.displayName),
            property: String(found.param.displayName),
            animated: safeCall(found.param, 'isTimeVarying', false),
            relative_to: relativeTo,
            count: out.length,
            keyframes: out
        };
    }

    function interpName(code) {
        for (var name in INTERP) {
            if (INTERP[name] === code) { return name; }
        }
        return String(code);
    }

    /** Bulk keyframe write -- the workhorse behind animate / bake / fades. */
    function setKeys(params) {
        var matches = T.resolve(params.clip);
        var relativeTo = params.relative_to || 'clip';
        var results = [];

        for (var m = 0; m < matches.length; m++) {
            var clip = matches[m].clip;
            var component = findComponent(clip, params.component, params.index);
            var param = findParam(component, params.property);

            if (params.replace) { clearKeys(param); }
            ensureTimeVarying(param);

            var written = 0;
            var pending = params.keyframes;
            for (var k = 0; k < pending.length; k++) {
                var entry = pending[k];
                var at = paramTime(clip, entry.time, relativeTo);
                var time = U.timeFromSeconds(at);

                var value = entry.value;
                if (entry.hold_current) {
                    // "start from wherever it is now" -- resolved here because
                    // only the host knows the current value.
                    try { value = param.getValue(); }
                    catch (e) { value = undefined; }
                }
                if (value === undefined) { continue; }

                try {
                    param.addKey(time);
                    param.setValueAtKey(time, coerce(param, value), true);
                    written++;
                } catch (e) {
                    throw U.fail(
                        'Failed writing keyframe ' + (k + 1) + ' of ' +
                        pending.length + ' on "' + param.displayName + '" at ' +
                        entry.time + 's: ' + e,
                        {
                            detail: { written: written, clip: String(clip.name) },
                            hint: 'Earlier keyframes in this call were written. '
                                + 'Read keyframe.list before retrying.'
                        }
                    );
                }

                if (entry.interp) { applyInterp(param, time, entry.interp); }
            }
            results[results.length] = {
                clip: String(clip.name),
                keyframes: written
            };
        }
        return { clips: results.length, results: results };
    }

    function applyInterp(param, time, name) {
        var code = INTERP[String(name).toLowerCase()];
        if (code === undefined) { return false; }
        try {
            param.setInterpolationTypeAtKey(time, code, true);
            return true;
        } catch (e) {
            // Interpolation is a refinement, not the substance of the edit --
            // a build that rejects a mode should not fail the whole animation.
            return false;
        }
    }

    function addKey(params) {
        var entry = {
            time: params.at,
            interp: params.interp
        };
        if (params.value !== undefined) {
            entry.value = params.value;
        } else {
            entry.hold_current = true;
        }
        return setKeys({
            clip: params.clip,
            component: params.component,
            property: params.property,
            index: params.index,
            relative_to: params.relative_to,
            keyframes: [entry]
        });
    }

    function removeKeys(params) {
        var matches = T.resolve(params.clip);
        var relativeTo = params.relative_to || 'clip';
        var removed = 0;

        for (var m = 0; m < matches.length; m++) {
            var clip = matches[m].clip;
            var component = findComponent(clip, params.component, params.index);
            var param = findParam(component, params.property);

            if (params.range) {
                var from = U.timeFromSeconds(
                    paramTime(clip, params.range[0], relativeTo));
                var to = U.timeFromSeconds(
                    paramTime(clip, params.range[1], relativeTo));
                try {
                    param.removeKeyRange(from, to, true);
                    removed++;
                } catch (e) {
                    // Fall back to removing them one at a time, which works on
                    // builds where removeKeyRange is missing or fussy.
                    var keys = keyTimes(param, clip, relativeTo);
                    for (var i = 0; i < keys.length; i++) {
                        if (keys[i].time >= Number(params.range[0]) &&
                            keys[i].time <= Number(params.range[1])) {
                            try { param.removeKey(keys[i].raw); removed++; }
                            catch (e2) { /* skip */ }
                        }
                    }
                }
            } else {
                var at = U.timeFromSeconds(paramTime(clip, params.at, relativeTo));
                try {
                    param.removeKey(at);
                    removed++;
                } catch (e3) {
                    throw U.fail(
                        'No keyframe to remove at ' + params.at + 's on "' +
                        param.displayName + '"',
                        { hint: 'keyframe.list shows the times that exist' }
                    );
                }
            }
        }
        return { removed: removed };
    }

    function clearKeys(param) {
        try {
            if (safeCall(param, 'isTimeVarying', false)) {
                param.setTimeVarying(false);
            }
            return true;
        } catch (e) {
            return false;
        }
    }

    function clear(params) {
        var matches = T.resolve(params.clip);
        var cleared = 0;
        for (var i = 0; i < matches.length; i++) {
            var component = findComponent(matches[i].clip, params.component,
                                          params.index);
            if (clearKeys(findParam(component, params.property))) { cleared++; }
        }
        return { cleared: cleared };
    }

    /**
     * Retime an existing animation.
     *
     * Read every keyframe, clear the parameter, rewrite at new times. There is
     * no move-keyframe API, and rewriting is the only ordering-safe approach --
     * shifting keyframes in place would collide with ones not yet moved.
     */
    function moveKeys(params) {
        var matches = T.resolve(params.clip);
        var relativeTo = params.relative_to || 'clip';
        var moved = 0;

        for (var m = 0; m < matches.length; m++) {
            var clip = matches[m].clip;
            var component = findComponent(clip, params.component, params.index);
            var param = findParam(component, params.property);

            var existing = keyTimes(param, clip, relativeTo);
            if (!existing.length) { continue; }

            var snapshot = [];
            for (var i = 0; i < existing.length; i++) {
                var inWindow = !params.range ||
                    (existing[i].time >= Number(params.range[0]) &&
                     existing[i].time <= Number(params.range[1]));
                var entry = { time: existing[i].time, move: inWindow };
                try { entry.value = param.getValueAtKey(existing[i].raw); }
                catch (e) { entry.value = undefined; }
                try {
                    entry.interp = interpName(
                        param.getInterpolationTypeAtKey(existing[i].raw));
                } catch (e2) { /* optional */ }
                snapshot[snapshot.length] = entry;
            }

            var anchor = snapshot[0].time;
            var rewritten = [];
            for (var s = 0; s < snapshot.length; s++) {
                var key = snapshot[s];
                var time = key.time;
                if (key.move) {
                    if (params.scale !== undefined) {
                        time = anchor + (time - anchor) * Number(params.scale);
                    }
                    if (params.by !== undefined) {
                        time = time + Number(params.by);
                    }
                    moved++;
                }
                if (key.value === undefined) { continue; }
                rewritten[rewritten.length] = {
                    time: time, value: key.value, interp: key.interp
                };
            }

            clearKeys(param);
            setKeysOn(param, clip, rewritten, relativeTo);
        }
        return { moved: moved };
    }

    function setKeysOn(param, clip, keys, relativeTo) {
        ensureTimeVarying(param);
        for (var i = 0; i < keys.length; i++) {
            var time = U.timeFromSeconds(
                paramTime(clip, keys[i].time, relativeTo));
            try {
                param.addKey(time);
                param.setValueAtKey(time, coerce(param, keys[i].value), true);
                if (keys[i].interp) { applyInterp(param, time, keys[i].interp); }
            } catch (e) { /* skip a key rather than abandon the rewrite */ }
        }
    }

    function setInterpolation(params) {
        var matches = T.resolve(params.clip);
        var relativeTo = params.relative_to || 'clip';
        var changed = 0;
        var failed = 0;

        for (var m = 0; m < matches.length; m++) {
            var clip = matches[m].clip;
            var component = findComponent(clip, params.component, params.index);
            var param = findParam(component, params.property);
            var keys = keyTimes(param, clip, relativeTo);

            for (var i = 0; i < keys.length; i++) {
                if (params.at !== undefined &&
                    Math.abs(keys[i].time - Number(params.at)) > 0.001) { continue; }
                if (params.range &&
                    (keys[i].time < Number(params.range[0]) ||
                     keys[i].time > Number(params.range[1]))) { continue; }
                if (applyInterp(param, keys[i].raw, params.interp)) { changed++; }
                else { failed++; }
            }
        }

        var result = { changed: changed };
        if (failed) {
            result.rejected = failed;
            result.note = 'This Premiere build rejected the "' + params.interp +
                '" interpolation mode on ' + failed + ' keyframe(s). For exact '
                + 'curve shapes use keyframe.bake, which does not rely on '
                + 'interpolation modes.';
        }
        return result;
    }

    return {
        INTERP: INTERP,
        findComponent: findComponent,
        findParam: findParam,
        paramTime: paramTime,
        fromParamTime: fromParamTime,
        safeCall: safeCall,
        get: get,
        set: set,
        reset: reset,
        listKeys: listKeys,
        setKeys: setKeys,
        addKey: addKey,
        removeKeys: removeKeys,
        clear: clear,
        moveKeys: moveKeys,
        setInterpolation: setInterpolation,
        interpName: interpName
    };
}());
