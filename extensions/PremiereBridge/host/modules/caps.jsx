/**
 * Capability discovery.
 *
 * Everything here is MEASURED against the running Premiere, not declared. That
 * is the whole point: the planning model should be able to ask "can this
 * install do X" and get an answer derived from the actual host, so it stops
 * inventing capabilities and stops avoiding ones that do exist.
 *
 * The interpolation probe genuinely writes keyframes to find out which
 * KFInterpMode constants a build accepts, then restores the parameter to the
 * static state it was in. It only ever touches a parameter that was NOT
 * already animated, so it cannot destroy an existing animation.
 */

var NovaCaps = (function () {
    var U = NovaUtil;
    var T = NovaTimeline;

    function probe(params) {
        var report = {
            app: 'Adobe Premiere Pro',
            version: String(app.version),
            build: safe(function () { return String(app.build); }, ''),
            project: safe(function () { return String(app.project.name); }, null),
            sequence: safe(function () {
                return String(app.project.activeSequence.name);
            }, null)
        };

        var qe = U.qeDom();
        report.qe = { available: !!qe };
        if (qe) {
            report.qe.effects = safe(function () {
                return Number(qe.project.numVideoEffects);
            }, 0);
            report.qe.audio_effects = safe(function () {
                return Number(qe.project.numAudioEffects);
            }, 0);
            report.qe.transitions = safe(function () {
                return Number(qe.project.numVideoTransitions);
            }, 0);
        } else {
            report.qe.note = 'QE is unavailable, so razor cuts, effect '
                + 'application, transitions and constant speed changes cannot '
                + 'run. Restarting Premiere usually restores it.';
        }

        var sequence = safe(function () { return app.project.activeSequence; }, null);
        report.apis = {
            importFiles: U.has(app.project, 'importFiles'),
            importMGT: !!(sequence && U.has(sequence, 'importMGT')),
            createCaptionTrack: !!(sequence && U.has(sequence, 'createCaptionTrack')),
            addTracks: !!(sequence && U.has(sequence, 'addTracks')),
            getSelection: !!(sequence && U.has(sequence, 'getSelection')),
            createNewSequenceFromClips:
                U.has(app.project, 'createNewSequenceFromClips'),
            saveAs: U.has(app.project, 'saveAs'),
            openDocument: U.has(app, 'openDocument')
        };

        if (sequence) {
            report.counts = {
                video_tracks: sequence.videoTracks.numTracks,
                audio_tracks: sequence.audioTracks.numTracks,
                fps: U.sequenceFps(sequence)
            };
            var size = U.frameSize(sequence);
            report.counts.width = size.width;
            report.counts.height = size.height;
        }

        report.interpolation = probeInterpolation();
        report.adjustment_layer = {
            available: safe(function () {
                NovaProject.findAsset('Adjustment Layer');
                return true;
            }, false),
            note: 'Premiere cannot create an adjustment layer by script. One '
                + 'made by hand in the project panel is detected and reused.'
        };

        return report;
    }

    function safe(fn, fallback) {
        try {
            var value = fn();
            return (value === undefined) ? fallback : value;
        } catch (e) {
            return fallback;
        }
    }

    /**
     * Find out which interpolation constants this build accepts.
     *
     * Returns the modes that round-tripped (set, then read back the same
     * value). A mode missing from `accepted` is not usable on this install --
     * which is exactly when the engine should bake a curve instead.
     */
    function probeInterpolation() {
        var result = {
            tested: false,
            accepted: [],
            rejected: [],
            note: ''
        };

        var candidate = findProbeParam();
        if (!candidate) {
            result.note = 'No clip with an idle keyframable parameter was '
                + 'available to test against. Interpolation support is unknown; '
                + 'keyframe.bake does not depend on it.';
            return result;
        }

        var param = candidate.param;
        var time = U.timeFromSeconds(
            NovaParams.paramTime(candidate.clip, 0, 'clip'));
        var original;
        try { original = param.getValue(); } catch (e) { original = null; }

        // Without a read-back getter we can set a mode but never confirm it
        // took. Reporting every mode as "rejected" in that case would be a
        // lie in the opposite direction, so say "unknown" instead.
        var canVerify = (typeof param.getInterpolationTypeAtKey === 'function');
        if (!canVerify) {
            result.note = 'This build exposes no getInterpolationTypeAtKey, so '
                + 'interpolation support could not be verified. Modes are still '
                + 'sent; use keyframe.bake when an exact curve matters.';
            result.verifiable = false;
            return result;
        }
        result.verifiable = true;

        try {
            param.setTimeVarying(true);
            param.addKey(time);
            if (original !== null) { param.setValueAtKey(time, original, true); }

            for (var name in NovaParams.INTERP) {
                if (!Object.prototype.hasOwnProperty.call(NovaParams.INTERP, name)) {
                    continue;
                }
                var code = NovaParams.INTERP[name];
                var ok = false;
                try {
                    param.setInterpolationTypeAtKey(time, code, true);
                    var readBack = param.getInterpolationTypeAtKey(time);
                    ok = (Number(readBack) === Number(code));
                } catch (e2) {
                    ok = false;
                }
                if (ok) { result.accepted[result.accepted.length] = name; }
                else { result.rejected[result.rejected.length] = name; }
            }
            result.tested = true;
        } catch (e3) {
            result.note = 'Interpolation probe failed: ' + e3;
        }

        // Restore: the parameter was static before we touched it, so turning
        // time-varying back off returns it exactly to its prior state.
        try {
            param.setTimeVarying(false);
            if (original !== null) { param.setValue(original, true); }
        } catch (e4) { /* best effort */ }

        if (result.tested && !result.note) {
            result.note = 'Measured on "' + candidate.clipName + '" > ' +
                candidate.componentName + ' > ' + candidate.paramName +
                '. Modes not listed in "accepted" are unavailable on this '
                + 'build; use keyframe.bake for exact curves.';
        }
        return result;
    }

    function findProbeParam() {
        var sequence = safe(function () { return app.project.activeSequence; }, null);
        if (!sequence) { return null; }

        for (var t = 0; t < sequence.videoTracks.numTracks; t++) {
            var track = sequence.videoTracks[t];
            for (var c = 0; c < track.clips.numItems; c++) {
                var clip = track.clips[c];
                var component, param;
                try {
                    component = NovaParams.findComponent(clip, 'Opacity', 0);
                    param = NovaParams.findParam(component, 'Opacity');
                } catch (e) { continue; }

                var keyframable = NovaParams.safeCall(param, 'areKeyframesSupported',
                                                      false);
                var animated = NovaParams.safeCall(param, 'isTimeVarying', false);
                // Never probe on a parameter the user has already animated.
                if (keyframable && !animated) {
                    return {
                        clip: clip, param: param,
                        clipName: String(clip.name),
                        componentName: 'Opacity',
                        paramName: 'Opacity'
                    };
                }
            }
        }
        return null;
    }

    /**
     * The real parameter list for a clip: names, values, types, and whether
     * each one can actually be keyframed.
     */
    function params(request) {
        var match = T.resolveOne(request.clip);
        var clip = match.clip;
        var includeValues = U.defined(request.include_values, true);
        var wanted = request.component;
        var components = [];

        for (var i = 0; i < clip.components.numItems; i++) {
            var component = clip.components[i];
            if (wanted && !U.nameMatches(component.displayName, wanted) &&
                !U.nameMatches(component.matchName, wanted)) {
                continue;
            }

            var entry = {
                name: String(component.displayName),
                match_name: String(component.matchName || ''),
                index: i,
                parameters: []
            };

            var count = 0;
            try { count = component.properties.numItems; } catch (e) { count = 0; }

            for (var p = 0; p < count; p++) {
                var param = component.properties[p];
                var info = { name: safe(function () {
                    return String(param.displayName);
                }, '(unnamed)') };

                info.keyframable = NovaParams.safeCall(param,
                                                       'areKeyframesSupported', false);
                info.animated = NovaParams.safeCall(param, 'isTimeVarying', false);

                if (includeValues) {
                    var value = safe(function () { return param.getValue(); }, undefined);
                    if (value !== undefined) {
                        info.value = value;
                        info.value_type = describeType(value);
                    } else {
                        info.readable = false;
                    }
                }
                if (info.animated) {
                    info.keyframes = safe(function () {
                        return param.getKeys().length;
                    }, 0);
                }
                entry.parameters[entry.parameters.length] = info;
            }
            components[components.length] = entry;
        }

        if (wanted && !components.length) {
            var available = [];
            for (var a = 0; a < clip.components.numItems; a++) {
                available[available.length] = String(clip.components[a].displayName);
            }
            throw U.fail('Clip "' + clip.name + '" has no component named "' +
                         wanted + '"', {
                hint: 'Available: ' + available.join(', '),
                detail: { available: available }
            });
        }

        return {
            clip: String(clip.name),
            track: T.trackLabel(match.type, match.trackIndex),
            components: components
        };
    }

    function describeType(value) {
        if (U.isArray(value)) { return 'list[' + value.length + ']'; }
        var type = typeof value;
        if (type === 'number') { return 'number'; }
        if (type === 'boolean') { return 'boolean'; }
        if (type === 'string') { return 'string'; }
        return type;
    }

    return { probe: probe, params: params };
}());
