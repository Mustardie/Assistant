/**
 * Effects, transitions and LUTs.
 *
 * Applying an effect is a QE-only operation -- the documented DOM lets you
 * READ a clip's components and drive their parameters, but gives no way to ADD
 * one. So effect.apply goes through QE, and everything afterwards (parameters,
 * keyframes, animation) uses the documented API via NovaParams. That split is
 * why the generic property system works on effect parameters at all.
 */

var NovaEffects = (function () {
    var U = NovaUtil;
    var T = NovaTimeline;

    // ------------------------------------------------------------------
    // Discovery
    // ------------------------------------------------------------------

    function listEffects(params) {
        var qe = U.requireQE('listing effects');
        var kind = params.kind || 'both';
        var search = params.search ? U.normalise(params.search) : '';
        var video = [];
        var audio = [];

        if (kind === 'video' || kind === 'both') {
            video = collect(qe.project, 'numVideoEffects', 'getVideoEffectByIndex',
                            search);
        }
        if (kind === 'audio' || kind === 'both') {
            audio = collect(qe.project, 'numAudioEffects', 'getAudioEffectByIndex',
                            search);
        }
        return {
            video: video, audio: audio,
            count: video.length + audio.length
        };
    }

    function listTransitions(params) {
        var qe = U.requireQE('listing transitions');
        var kind = params.kind || 'both';
        var search = params.search ? U.normalise(params.search) : '';
        var video = [];
        var audio = [];

        if (kind === 'video' || kind === 'both') {
            video = collect(qe.project, 'numVideoTransitions',
                            'getVideoTransitionByIndex', search);
        }
        if (kind === 'audio' || kind === 'both') {
            audio = collect(qe.project, 'numAudioTransitions',
                            'getAudioTransitionByIndex', search);
        }
        return {
            video: video, audio: audio,
            count: video.length + audio.length
        };
    }

    function collect(project, countMember, getterName, search) {
        var out = [];
        var total = 0;
        try { total = Number(project[countMember]); } catch (e) { return out; }
        if (!total || isNaN(total)) { return out; }

        for (var i = 0; i < total; i++) {
            try {
                var entry = project[getterName](i);
                if (!entry) { continue; }
                var name = String(entry.name);
                if (search && U.normalise(name).indexOf(search) === -1) { continue; }
                out[out.length] = name;
            } catch (e2) { /* skip entries that throw */ }
        }
        out.sort();
        return out;
    }

    function findEffect(qe, name, isAudio) {
        var getter = isAudio ? 'getAudioEffectByName' : 'getVideoEffectByName';
        var effect = null;
        try { effect = qe.project[getter](String(name)); } catch (e) { effect = null; }
        if (effect) { return effect; }

        // Case/spacing-insensitive second pass, so "gaussian blur" finds
        // "Gaussian Blur" instead of failing on capitalisation.
        var countMember = isAudio ? 'numAudioEffects' : 'numVideoEffects';
        var indexGetter = isAudio ? 'getAudioEffectByIndex' : 'getVideoEffectByIndex';
        var total = 0;
        try { total = Number(qe.project[countMember]); } catch (e2) { total = 0; }
        var near = [];
        for (var i = 0; i < total; i++) {
            try {
                var candidate = qe.project[indexGetter](i);
                if (!candidate) { continue; }
                if (U.nameMatches(candidate.name, name)) { return candidate; }
                if (U.normalise(candidate.name).indexOf(U.normalise(name)) !== -1) {
                    near[near.length] = String(candidate.name);
                }
            } catch (e3) { /* skip */ }
        }

        throw U.fail('No ' + (isAudio ? 'audio' : 'video') + ' effect named "' +
                     name + '" is installed', {
            hint: near.length
                ? 'Close matches: ' + near.slice(0, 8).join(', ')
                : 'Call caps.effects for the exact installed names',
            detail: { near: near.slice(0, 20) }
        });
    }

    // ------------------------------------------------------------------
    // Apply / remove
    // ------------------------------------------------------------------

    function apply(params) {
        var qe = U.requireQE('applying an effect');
        var qeSeq = U.qeSequence();
        var matches = T.resolve(params.clip);
        var applied = [];

        for (var i = 0; i < matches.length; i++) {
            var match = matches[i];
            var isAudio = (match.type === 'audio');

            var existing = null;
            if (params.reuse) {
                // color.grade calls this repeatedly; stacking a second Lumetri
                // on every call would be wrong and slow.
                try {
                    existing = NovaParams.findComponent(match.clip, params.effect, 0);
                } catch (e) { existing = null; }
            }

            if (!existing) {
                var effect = findEffect(qe, params.effect, isAudio);
                var qeTrack = isAudio
                    ? qeSeq.getAudioTrackAt(match.trackIndex)
                    : qeSeq.getVideoTrackAt(match.trackIndex);
                var qeClip = NovaClips.qeItemFor(qeTrack, match.clip);
                if (!qeClip) {
                    throw U.fail('QE could not find "' + match.clip.name +
                                 '" to apply "' + params.effect + '" to', {
                        hint: 'Re-read timeline.snapshot; QE indices shift when '
                            + 'transitions are present.'
                    });
                }
                try {
                    if (isAudio) { qeClip.addAudioEffect(effect); }
                    else { qeClip.addVideoEffect(effect); }
                } catch (e2) {
                    throw U.fail('Could not apply "' + params.effect + '" to "' +
                                 match.clip.name + '": ' + e2);
                }
            }

            var result = {
                clip: String(match.clip.name),
                effect: String(params.effect),
                reused: !!existing
            };

            if (params.params) {
                result.params = setParams(match.clip, params.effect, params.params);
            }
            applied[applied.length] = result;
        }
        return { applied: applied.length, clips: applied };
    }

    /**
     * Set several parameters on a just-applied effect.
     *
     * Unknown parameter names are collected and returned rather than thrown:
     * setting eight Lumetri knobs of which one has been renamed should apply
     * the seven that exist and tell you about the eighth.
     */
    function setParams(clip, effectName, values) {
        var component = NovaParams.findComponent(clip, effectName, 0);
        var applied = {};
        var missing = [];

        for (var key in values) {
            if (!Object.prototype.hasOwnProperty.call(values, key)) { continue; }
            var param = null;
            try {
                param = NovaParams.findParam(component, key);
            } catch (e) {
                missing[missing.length] = key;
                continue;
            }
            try {
                param.setValue(values[key], true);
                applied[key] = values[key];
            } catch (e2) {
                missing[missing.length] = key;
            }
        }

        var out = { applied: applied };
        if (missing.length) {
            out.not_applied = missing;
            out.available = paramNames(component);
            out.note = 'These parameters do not exist (or are read-only) on "' +
                       effectName + '" in this Premiere build: ' +
                       missing.join(', ');
        }
        return out;
    }

    function paramNames(component) {
        var names = [];
        try {
            for (var i = 0; i < component.properties.numItems; i++) {
                names[names.length] = String(component.properties[i].displayName);
            }
        } catch (e) { /* unreadable */ }
        return names;
    }

    function list(params) {
        var match = T.resolveOne(params.clip);
        var out = [];
        for (var i = 0; i < match.clip.components.numItems; i++) {
            var component = match.clip.components[i];
            out[out.length] = {
                index: i,
                name: String(component.displayName),
                match_name: String(component.matchName || ''),
                parameters: paramNames(component)
            };
        }
        return { clip: String(match.clip.name), count: out.length, effects: out };
    }

    /**
     * Remove effects.
     *
     * The first three components of a clip are its intrinsics (Motion, Opacity,
     * Time Remapping on video; Volume, Channel Volume, Panner on audio).
     * Removing those would break the clip, so they are never touched.
     */
    function remove(params) {
        var matches = T.resolve(params.clip);
        var removed = 0;
        var failures = [];

        for (var m = 0; m < matches.length; m++) {
            var clip = matches[m].clip;
            var components = clip.components;

            for (var i = components.numItems - 1; i >= 0; i--) {
                var component = components[i];
                var name = String(component.displayName);
                if (isIntrinsic(name)) { continue; }

                var wanted = params.all ||
                    (params.effect && U.nameMatches(name, params.effect)) ||
                    (params.index !== undefined && Number(params.index) === i);
                if (!wanted) { continue; }

                try {
                    component.remove();
                    removed++;
                } catch (e) {
                    failures[failures.length] = { effect: name, error: String(e) };
                }
            }
        }

        if (!removed && failures.length) {
            throw U.unsupported(
                'This Premiere build does not allow removing effects by script',
                'remove the effect in the Effect Controls panel, or set its '
                    + 'parameters back to neutral with property.set',
                { failures: failures }
            );
        }
        var result = { removed: removed };
        if (failures.length) { result.failed = failures; }
        if (!removed && !failures.length) {
            result.note = 'No matching removable effect was found on the clip.';
        }
        return result;
    }

    function isIntrinsic(name) {
        var intrinsics = ['Motion', 'Opacity', 'Time Remapping', 'Volume',
                          'Channel Volume', 'Panner', 'Audio Effects',
                          'Video Effects'];
        for (var i = 0; i < intrinsics.length; i++) {
            if (U.nameMatches(name, intrinsics[i])) { return true; }
        }
        return false;
    }

    // ------------------------------------------------------------------
    // Transitions
    // ------------------------------------------------------------------

    function applyTransition(params) {
        var qe = U.requireQE('applying a transition');
        var qeSeq = U.qeSequence();
        var sequence = U.activeSequence();
        var fps = U.sequenceFps(sequence);
        var matches = T.resolve(params.clip);
        var duration = Number(params.duration || 1.0);
        var name = params.transition || 'Cross Dissolve';
        var applied = [];
        var failures = [];

        for (var i = 0; i < matches.length; i++) {
            var match = matches[i];
            var isAudio = (match.type === 'audio');
            var transition = findTransition(qe, name, isAudio);
            var qeTrack = isAudio
                ? qeSeq.getAudioTrackAt(match.trackIndex)
                : qeSeq.getVideoTrackAt(match.trackIndex);
            var qeClip = NovaClips.qeItemFor(qeTrack, match.clip);
            if (!qeClip) {
                failures[failures.length] = {
                    clip: String(match.clip.name), reason: 'not visible to QE'
                };
                continue;
            }

            var edges = (params.edge === 'both') ? ['in', 'out'] : [params.edge || 'out'];
            for (var e = 0; e < edges.length; e++) {
                var atStart = (edges[e] === 'in');
                if (addTransition(qeClip, transition, atStart, duration, fps,
                                  params.alignment)) {
                    applied[applied.length] = {
                        clip: String(match.clip.name),
                        edge: edges[e],
                        transition: String(name),
                        duration: duration
                    };
                } else {
                    failures[failures.length] = {
                        clip: String(match.clip.name), edge: edges[e],
                        reason: 'QE rejected the transition'
                    };
                }
            }
        }

        if (!applied.length) {
            throw U.fail('Could not apply "' + name + '"', {
                hint: 'Transitions need media handles beyond the cut. Trim the '
                    + 'clips slightly, or shorten the transition duration.',
                detail: { failures: failures }
            });
        }
        var result = { applied: applied.length, transitions: applied };
        if (failures.length) { result.failed = failures; }
        return result;
    }

    /**
     * QE's addTransition signature has changed across versions and is
     * undocumented in all of them. Rather than pin one shape, try the known
     * forms in order of specificity and take the first that works.
     */
    function addTransition(qeClip, transition, atStart, duration, fps, alignment) {
        var timecode = U.timecode(duration, fps);
        var alignmentCode = alignmentToCode(alignment);
        var attempts = [
            function () {
                return qeClip.addTransition(transition, atStart, timecode,
                                            alignmentCode);
            },
            function () {
                return qeClip.addTransition(transition, atStart, timecode);
            },
            function () {
                return qeClip.addTransition(transition, atStart);
            },
            function () {
                return qeClip.addVideoTransition(transition, atStart, timecode);
            },
            function () {
                return qeClip.addAudioTransition(transition, atStart, timecode);
            }
        ];
        for (var i = 0; i < attempts.length; i++) {
            try {
                var outcome = attempts[i]();
                if (outcome !== false) { return true; }
            } catch (e) { /* try the next shape */ }
        }
        return false;
    }

    function alignmentToCode(alignment) {
        if (alignment === 'start') { return 0; }
        if (alignment === 'end') { return 2; }
        return 1;   // centred on the cut
    }

    function findTransition(qe, name, isAudio) {
        var getter = isAudio ? 'getAudioTransitionByName' : 'getVideoTransitionByName';
        try {
            var found = qe.project[getter](String(name));
            if (found) { return found; }
        } catch (e) { /* fall through to the scan */ }

        var countMember = isAudio ? 'numAudioTransitions' : 'numVideoTransitions';
        var indexGetter = isAudio ? 'getAudioTransitionByIndex'
                                  : 'getVideoTransitionByIndex';
        var total = 0;
        try { total = Number(qe.project[countMember]); } catch (e2) { total = 0; }
        var near = [];
        for (var i = 0; i < total; i++) {
            try {
                var candidate = qe.project[indexGetter](i);
                if (!candidate) { continue; }
                if (U.nameMatches(candidate.name, name)) { return candidate; }
                if (U.normalise(candidate.name).indexOf(U.normalise(name)) !== -1) {
                    near[near.length] = String(candidate.name);
                }
            } catch (e3) { /* skip */ }
        }
        throw U.fail('No transition named "' + name + '" is installed', {
            hint: near.length ? 'Close matches: ' + near.slice(0, 8).join(', ')
                              : 'Call caps.transitions for the installed names'
        });
    }

    function removeTransition(params) {
        // Transitions live on the track between clips; the DOM has no handle on
        // them and QE offers no remove on any shipping build.
        throw U.unsupported(
            'Removing a transition is not exposed by the Premiere scripting API',
            'select the transition in the timeline and press Delete, or rebuild '
                + 'the cut with clip.trim'
        );
    }

    // ------------------------------------------------------------------
    // LUTs
    // ------------------------------------------------------------------

    /**
     * Assign a .cube LUT to Lumetri.
     *
     * Lumetri's LUT slots are dropdown parameters populated from Premiere's own
     * LUT folders. Whether a filesystem path can be assigned to one varies by
     * build, so this attempts it and reports honestly when it cannot -- rather
     * than applying Lumetri, changing nothing, and returning success.
     */
    function applyLut(params) {
        var matches = T.resolve(params.clip);
        var slotName = (params.lut && params.lut.slot === 'input')
            ? 'Input LUT' : 'Creative Look';
        var path = params.lut.path;

        if (!File(path).exists) {
            throw U.fail('LUT file not found: ' + path, {
                hint: 'The path must be absolute and on the Premiere machine'
            });
        }

        apply({ clip: params.clip, effect: 'Lumetri Color', reuse: true });

        var results = [];
        var failures = [];
        for (var i = 0; i < matches.length; i++) {
            var clip = matches[i].clip;
            var component = NovaParams.findComponent(clip, 'Lumetri Color', 0);
            var param = null;
            try {
                param = NovaParams.findParam(component, slotName);
            } catch (e) {
                failures[failures.length] = {
                    clip: String(clip.name),
                    reason: '"' + slotName + '" is not exposed on this build',
                    available: paramNames(component)
                };
                continue;
            }
            try {
                param.setValue(String(path), true);
                results[results.length] = { clip: String(clip.name), lut: path };
            } catch (e2) {
                failures[failures.length] = {
                    clip: String(clip.name), reason: String(e2)
                };
            }
        }

        if (!results.length) {
            throw U.unsupported(
                'This Premiere build will not accept a LUT path through the '
                    + 'Lumetri "' + slotName + '" parameter',
                'copy the .cube into Premiere\'s LUT folder and select it by '
                    + 'hand, or grade with color.grade parameters instead',
                { failures: failures }
            );
        }
        var out = { applied: results.length, clips: results };
        if (failures.length) { out.failed = failures; }
        return out;
    }

    return {
        listEffects: listEffects,
        listTransitions: listTransitions,
        apply: apply,
        list: list,
        remove: remove,
        setParams: setParams,
        paramNames: paramNames,
        applyTransition: applyTransition,
        removeTransition: removeTransition,
        applyLut: applyLut
    };
}());
