/**
 * Timeline reading, clip resolution, tracks and revisions.
 *
 * Clip resolution is the foundation everything else stands on: a selector like
 * {"track": "V1", "at": 12.5} has to become a real TrackItem before any
 * operation can touch it. Getting this wrong silently edits the wrong clip, so
 * resolution is strict -- it throws with the actual track contents attached
 * rather than returning the nearest guess.
 */

var NovaTimeline = (function () {
    var U = NovaUtil;

    // ------------------------------------------------------------------
    // Tracks
    // ------------------------------------------------------------------

    function trackCollection(sequence, type) {
        return (type === 'audio') ? sequence.audioTracks : sequence.videoTracks;
    }

    /**
     * Parse "V2" / "A3" into {type, index}.
     *
     * The Python layer normalises track references before sending, so the host
     * used to assume the object form and quietly fall back to video track 0
     * for anything else. That is the worst possible failure: a caller that
     * sent the UI spelling got an operation applied to the programme track
     * with no error at all. Accepting both spellings here means the host is
     * correct on its own rather than only in company.
     */
    function parseTrack(value) {
        var text = String(value === undefined || value === null ? '' : value)
            .replace(/^\s+|\s+$/g, '').toUpperCase();
        var letter = text.charAt(0);
        var number = text.substring(1);
        if ((letter !== 'V' && letter !== 'A') || !/^[0-9]+$/.test(number)) {
            return null;
        }
        var index = parseInt(number, 10) - 1;   // "V1" is index 0
        if (index < 0) { return null; }
        return { type: (letter === 'A') ? 'audio' : 'video', index: index };
    }

    function getTrack(sequence, spec) {
        if (typeof spec === 'string') {
            var parsed = parseTrack(spec);
            if (!parsed) {
                throw U.fail('Unrecognised track "' + spec + '"', {
                    code: 'validation_error',
                    hint: 'Use "V1"/"V2" for video or "A1"/"A2" for audio.'
                });
            }
            spec = parsed;
        }
        var type = (spec && spec.type) ? spec.type : 'video';
        var index = (spec && spec.index !== undefined) ? Number(spec.index) : 0;
        var tracks = trackCollection(sequence, type);
        if (!tracks || index < 0 || index >= tracks.numTracks) {
            throw U.fail(
                'Track ' + (type === 'audio' ? 'A' : 'V') + (index + 1) +
                ' does not exist',
                {
                    hint: 'This sequence has ' + sequence.videoTracks.numTracks +
                          ' video and ' + sequence.audioTracks.numTracks +
                          ' audio tracks. Use track.add to create more.',
                    detail: {
                        video_tracks: sequence.videoTracks.numTracks,
                        audio_tracks: sequence.audioTracks.numTracks
                    }
                }
            );
        }
        return tracks[index];
    }

    function trackLabel(type, index) {
        return (type === 'audio' ? 'A' : 'V') + (index + 1);
    }

    // ------------------------------------------------------------------
    // Clip identity
    // ------------------------------------------------------------------

    /**
     * A stable-enough id for a clip.
     *
     * nodeId is Premiere's own per-item identifier and survives moves, so it is
     * always preferred. The composed fallback (track + start ticks) is only
     * used on builds that do not expose it, and is regenerated on every
     * snapshot -- which is why every operation that needs to act on a clip
     * twice resolves it first rather than caching an id across batches.
     */
    function clipId(clip, type, trackIndex, clipIndex) {
        try {
            if (clip.nodeId) { return String(clip.nodeId); }
        } catch (e) { /* fall through */ }
        return trackLabel(type, trackIndex) + ':' + clipIndex + ':' +
               Math.round(U.secondsOf(clip.start) * 1000);
    }

    function describeClip(clip, type, trackIndex, clipIndex, options) {
        var opts = options || {};
        var info = {
            id: clipId(clip, type, trackIndex, clipIndex),
            index: clipIndex,
            track: trackLabel(type, trackIndex),
            track_type: type,
            track_index: trackIndex,
            name: String(clip.name || ''),
            start: U.secondsOf(clip.start),
            end: U.secondsOf(clip.end),
            duration: U.secondsOf(clip.duration),
            in_point: U.secondsOf(clip.inPoint),
            out_point: U.secondsOf(clip.outPoint)
        };
        try { info.enabled = (clip.disabled !== true); } catch (e) { /* optional */ }
        try { info.type = clip.mediaType; } catch (e) { /* optional */ }
        try {
            var speed = clip.getSpeed();
            if (speed !== undefined && speed !== null) { info.speed = Number(speed); }
        } catch (e) { /* getSpeed is not on every build */ }
        try {
            if (clip.projectItem) {
                info.source = String(clip.projectItem.name);
                try {
                    info.source_path = String(clip.projectItem.getMediaPath());
                } catch (e2) { /* synthetics have no media path */ }
            }
        } catch (e3) { /* optional */ }

        if (opts.include_effects) {
            info.effects = [];
            try {
                for (var i = 0; i < clip.components.numItems; i++) {
                    var component = clip.components[i];
                    var entry = {
                        name: String(component.displayName),
                        match_name: String(component.matchName || ''),
                        index: i,
                        params: component.properties ? component.properties.numItems : 0
                    };
                    if (opts.include_keyframes) {
                        entry.animated = animatedParams(component);
                    }
                    info.effects[info.effects.length] = entry;
                }
            } catch (e4) {
                info.effects_error = String(e4);
            }
        }
        return info;
    }

    function animatedParams(component) {
        var animated = [];
        try {
            for (var i = 0; i < component.properties.numItems; i++) {
                var param = component.properties[i];
                try {
                    if (param.isTimeVarying && param.isTimeVarying()) {
                        var keys = [];
                        try {
                            var times = param.getKeys();
                            for (var k = 0; k < times.length; k++) {
                                keys[keys.length] = U.secondsOf(times[k]);
                            }
                        } catch (e) { /* keys unreadable on some params */ }
                        animated[animated.length] = {
                            name: String(param.displayName),
                            keyframes: keys.length,
                            times: keys
                        };
                    }
                } catch (e2) { /* skip params that throw on inspection */ }
            }
        } catch (e3) { /* component has no readable properties */ }
        return animated;
    }

    // ------------------------------------------------------------------
    // Resolution
    // ------------------------------------------------------------------

    /** Resolve a selector to [{clip, type, trackIndex, clipIndex}]. */
    function resolve(selector, sequence) {
        var seq = sequence || U.activeSequence();
        var matches = [];

        if (!selector) {
            throw U.fail('No clip selector given', { code: 'validation_error' });
        }

        // By id: scan every track. Linear, but a sequence has hundreds of clips
        // at most and correctness beats cleverness here.
        if (selector.id) {
            matches = scanAll(seq, function (clip, type, trackIndex, clipIndex) {
                return clipId(clip, type, trackIndex, clipIndex) === String(selector.id);
            });
            if (!matches.length) {
                throw U.fail('No clip with id ' + selector.id, {
                    hint: 'Clip ids change when clips move. Re-read '
                        + 'timeline.snapshot and use the current id.'
                });
            }
            return matches;
        }

        if (selector.selected) {
            var selection = seq.getSelection();
            for (var s = 0; s < selection.length; s++) {
                var found = locate(seq, selection[s]);
                if (found) { matches[matches.length] = found; }
            }
            if (!matches.length) {
                throw U.fail('Nothing is selected in the timeline', {
                    hint: 'Select clips in Premiere, or use an explicit selector'
                });
            }
            return matches;
        }

        // Name-only search across every track.
        if (selector.name && !selector.track) {
            matches = scanAll(seq, function (clip) {
                return U.nameMatches(clip.name, selector.name);
            });
            if (!matches.length) {
                throw U.fail('No clip named "' + selector.name + '"', {
                    hint: 'Names come from timeline.snapshot'
                });
            }
            return matches;
        }

        // Same reason as getTrack: accept the UI spelling rather than
        // silently resolving to video track 0.
        var spec = selector.track;
        if (typeof spec === 'string') {
            spec = parseTrack(spec);
            if (!spec) {
                throw U.fail('Unrecognised track "' + selector.track + '"', {
                    code: 'validation_error',
                    hint: 'Use "V1"/"V2" for video or "A1"/"A2" for audio.'
                });
            }
        }
        var type = (spec && spec.type) || 'video';
        var trackIndex = (spec && spec.index !== undefined)
            ? Number(spec.index) : 0;
        var track = getTrack(seq, { type: type, index: trackIndex });
        var count = track.clips.numItems;

        function push(clipIndex) {
            matches[matches.length] = {
                clip: track.clips[clipIndex],
                type: type,
                trackIndex: trackIndex,
                clipIndex: clipIndex
            };
        }

        if (selector.index !== undefined) {
            var wanted = Number(selector.index);
            if (wanted >= count) {
                throw U.fail(
                    trackLabel(type, trackIndex) + ' has ' + count +
                    ' clip(s); index ' + wanted + ' is out of range',
                    {
                        hint: count ? 'Valid indices are 0 to ' + (count - 1)
                                    : 'This track is empty',
                        detail: { clips: count }
                    }
                );
            }
            push(wanted);
            return matches;
        }

        if (selector.at !== undefined) {
            var at = Number(selector.at);
            for (var i = 0; i < count; i++) {
                var clip = track.clips[i];
                if (U.secondsOf(clip.start) <= at && U.secondsOf(clip.end) > at) {
                    push(i);
                    return matches;
                }
            }
            throw U.fail(
                'No clip on ' + trackLabel(type, trackIndex) + ' at ' + at + 's',
                { hint: 'timeline.snapshot shows where the clips actually are',
                  detail: { clips: count } }
            );
        }

        if (selector.range) {
            var from = Number(selector.range[0]);
            var to = Number(selector.range[1]);
            for (var r = 0; r < count; r++) {
                var candidate = track.clips[r];
                // Overlap, not containment: a clip straddling the window edge is
                // part of that window for editing purposes.
                if (U.secondsOf(candidate.end) > from &&
                    U.secondsOf(candidate.start) < to) {
                    if (!selector.name || U.nameMatches(candidate.name, selector.name)) {
                        push(r);
                    }
                }
            }
            if (!matches.length) {
                throw U.fail(
                    'No clips on ' + trackLabel(type, trackIndex) +
                    ' between ' + from + 's and ' + to + 's', {}
                );
            }
            return matches;
        }

        if (selector.all) {
            for (var a = 0; a < count; a++) {
                if (!selector.name || U.nameMatches(track.clips[a].name, selector.name)) {
                    push(a);
                }
            }
            if (!matches.length) {
                throw U.fail(trackLabel(type, trackIndex) + ' has no clips', {});
            }
            return matches;
        }

        if (selector.name) {
            for (var n = 0; n < count; n++) {
                if (U.nameMatches(track.clips[n].name, selector.name)) { push(n); }
            }
            if (!matches.length) {
                throw U.fail(
                    'No clip named "' + selector.name + '" on ' +
                    trackLabel(type, trackIndex), {}
                );
            }
            return matches;
        }

        throw U.fail('Clip selector did not identify anything', {
            code: 'validation_error',
            hint: 'Use index, at, range, all, id, name or selected'
        });
    }

    function scanAll(sequence, predicate) {
        var found = [];
        var kinds = ['video', 'audio'];
        for (var k = 0; k < kinds.length; k++) {
            var type = kinds[k];
            var tracks = trackCollection(sequence, type);
            for (var t = 0; t < tracks.numTracks; t++) {
                var clips = tracks[t].clips;
                for (var c = 0; c < clips.numItems; c++) {
                    if (predicate(clips[c], type, t, c)) {
                        found[found.length] = {
                            clip: clips[c], type: type, trackIndex: t, clipIndex: c
                        };
                    }
                }
            }
        }
        return found;
    }

    function locate(sequence, target) {
        var found = scanAll(sequence, function (clip) {
            try {
                return clip.nodeId && target.nodeId && clip.nodeId === target.nodeId;
            } catch (e) {
                return false;
            }
        });
        return found.length ? found[0] : null;
    }

    function resolveOne(selector, sequence) {
        var matches = resolve(selector, sequence);
        return matches[0];
    }

    // ------------------------------------------------------------------
    // Operations
    // ------------------------------------------------------------------

    function snapshot(params) {
        var sequence = U.activeSequence();
        var options = {
            include_effects: U.defined(params.include_effects, true),
            include_keyframes: U.defined(params.include_keyframes, false)
        };
        var range = params.range;
        var wantTrack = params.track;

        var out = {
            sequence: String(sequence.name),
            fps: U.sequenceFps(sequence),
            duration: U.secondsOf(sequence.end),
            tracks: []
        };
        var size = U.frameSize(sequence);
        out.width = size.width;
        out.height = size.height;

        var kinds = ['video', 'audio'];
        for (var k = 0; k < kinds.length; k++) {
            var type = kinds[k];
            if (wantTrack && wantTrack.type && wantTrack.type !== type) { continue; }
            var tracks = trackCollection(sequence, type);
            for (var t = 0; t < tracks.numTracks; t++) {
                if (wantTrack && wantTrack.index !== undefined &&
                    Number(wantTrack.index) !== t) { continue; }
                var track = tracks[t];
                var entry = {
                    track: trackLabel(type, t),
                    type: type,
                    index: t,
                    name: safeString(track, 'name'),
                    muted: safeBool(track, 'isMuted'),
                    locked: safeBool(track, 'isLocked'),
                    targeted: safeBool(track, 'isTargeted'),
                    clips: []
                };
                for (var c = 0; c < track.clips.numItems; c++) {
                    var clip = track.clips[c];
                    if (range) {
                        if (U.secondsOf(clip.end) <= Number(range[0]) ||
                            U.secondsOf(clip.start) >= Number(range[1])) {
                            continue;
                        }
                    }
                    entry.clips[entry.clips.length] =
                        describeClip(clip, type, t, c, options);
                }
                out.tracks[out.tracks.length] = entry;
            }
        }
        out.revision = revisionOf(sequence).revision;
        return out;
    }

    function safeString(object, member) {
        try { return String(object[member]); } catch (e) { return ''; }
    }

    function safeBool(object, member) {
        try {
            var value = object[member];
            return (typeof value === 'function') ? !!object[member]() : !!value;
        } catch (e) { return false; }
    }

    /**
     * A cheap fingerprint of the timeline's structure.
     *
     * Used for revision protection: if this changes between the model reading
     * the timeline and its plan executing, clip indices in the plan may no
     * longer mean what the model intended, and the plan is refused.
     */
    function revisionOf(sequence) {
        var seq = sequence || app.project.activeSequence;
        if (!seq) { return { revision: null }; }
        var parts = [String(seq.name)];
        var kinds = ['video', 'audio'];
        for (var k = 0; k < kinds.length; k++) {
            var tracks = trackCollection(seq, kinds[k]);
            for (var t = 0; t < tracks.numTracks; t++) {
                var clips = tracks[t].clips;
                parts[parts.length] = kinds[k] + t + '#' + clips.numItems;
                for (var c = 0; c < clips.numItems; c++) {
                    parts[parts.length] =
                        Math.round(U.secondsOf(clips[c].start) * 1000) + '-' +
                        Math.round(U.secondsOf(clips[c].end) * 1000);
                }
            }
        }
        return { revision: hash(parts.join('|')), sequence: String(seq.name) };
    }

    function hash(text) {
        // djb2. Only needs to detect change, not resist collisions.
        var value = 5381;
        for (var i = 0; i < text.length; i++) {
            value = ((value * 33) ^ text.charCodeAt(i)) & 0x7fffffff;
        }
        return value.toString(36);
    }

    function playhead(params) {
        var sequence = U.activeSequence();
        var result = {};
        if (params.time !== undefined) {
            sequence.setPlayerPosition(U.ticksOf(params.time));
            result.time = Number(params.time);
        }
        if (params['in'] !== undefined) {
            try {
                sequence.setInPoint(U.ticksOf(params['in']));
                result['in'] = Number(params['in']);
            } catch (e) {
                throw U.fail('Could not set the sequence in point: ' + e);
            }
        }
        if (params.out !== undefined) {
            try {
                sequence.setOutPoint(U.ticksOf(params.out));
                result.out = Number(params.out);
            } catch (e) {
                throw U.fail('Could not set the sequence out point: ' + e);
            }
        }
        return result;
    }

    /**
     * Add video and/or audio tracks.
     *
     * Three routes, tried in order, because the scriptable API for this has
     * changed and no single call works everywhere. On Premiere 25
     * `Sequence.addTracks` is simply not a function, which is what stopped
     * every overlay in this system from having a track to land on:
     *
     *   1. `Sequence.addTracks(...)` -- the documented call, present on older
     *      builds.
     *   2. the QE DOM's `addTracks(numVideo, videoIndex, numAudio,
     *      audioIndex, ...)`, which is what Premiere's own UI drives.
     *   3. `qe.addTracks(n)` per track type, the oldest spelling.
     *
     * Success is confirmed by counting tracks afterwards rather than by the
     * absence of an exception: two of these routes return nothing at all, and
     * one of them silently does nothing on a locked sequence.
     */
    function addTracks(params) {
        var sequence = U.activeSequence();
        var video = Math.max(0, Number(params.video || 0));
        var audio = Math.max(0, Number(params.audio || 0));
        if (!video && !audio) {
            return { added: { video: 0, audio: 0 },
                     video_tracks: sequence.videoTracks.numTracks,
                     audio_tracks: sequence.audioTracks.numTracks,
                     method: 'noop' };
        }

        var before = {
            video: sequence.videoTracks.numTracks,
            audio: sequence.audioTracks.numTracks
        };
        var videoIndex = (params.at !== undefined) ? Number(params.at)
                                                   : before.video;
        var audioIndex = (params.at !== undefined) ? Number(params.at)
                                                   : before.audio;
        var errors = [];

        function grew() {
            return sequence.videoTracks.numTracks >= before.video + video
                && sequence.audioTracks.numTracks >= before.audio + audio;
        }

        // 1. The documented Sequence call. audioChannelType 1 is stereo; the
        //    submix arguments must be present or the call is rejected.
        try {
            if (typeof sequence.addTracks === 'function') {
                sequence.addTracks(video, videoIndex, audio, audioIndex, 1, 0, 0);
                if (grew()) { return report(before, video, audio, 'sequence'); }
                errors[errors.length] = 'Sequence.addTracks added no tracks';
            } else {
                errors[errors.length] = 'Sequence.addTracks is not a function';
            }
        } catch (e) {
            errors[errors.length] = 'Sequence.addTracks: ' + (e.message || e);
        }

        // 2. The QE DOM, which is what the application's own menu item uses.
        try {
            var qeSeq = U.qeSequence();
            if (qeSeq && typeof qeSeq.addTracks === 'function') {
                qeSeq.addTracks(video, videoIndex, audio, audioIndex, 1, 0, 0);
                if (grew()) { return report(before, video, audio, 'qe'); }
                errors[errors.length] = 'qe addTracks added no tracks';
            } else {
                errors[errors.length] = 'the QE sequence has no addTracks';
            }
        } catch (e2) {
            errors[errors.length] = 'qe addTracks: ' + (e2.message || e2);
        }

        // 3. The oldest QE spelling: one count, one type at a time.
        try {
            var dom = U.qeDom();
            var qeSeq2 = dom ? dom.project.getActiveSequence() : null;
            if (qeSeq2) {
                if (video && typeof qeSeq2.addVideoTrack === 'function') {
                    for (var v = 0; v < video; v++) { qeSeq2.addVideoTrack(); }
                }
                if (audio && typeof qeSeq2.addAudioTrack === 'function') {
                    for (var a = 0; a < audio; a++) { qeSeq2.addAudioTrack(); }
                }
                if (grew()) { return report(before, video, audio, 'qe_single'); }
                errors[errors.length] = 'per-track QE calls added no tracks';
            }
        } catch (e3) {
            errors[errors.length] = 'per-track QE: ' + (e3.message || e3);
        }

        throw U.fail('Could not add tracks to "' + String(sequence.name) + '"', {
            hint: 'The sequence may be locked, or this Premiere build may '
                + 'expose no scriptable way to add a track. Add the tracks by '
                + 'hand once and re-run: the plan is idempotent and will skip '
                + 'this step when the sequence is already tall enough.',
            detail: {
                attempts: errors,
                wanted: { video: video, audio: audio },
                have: { video: sequence.videoTracks.numTracks,
                        audio: sequence.audioTracks.numTracks }
            }
        });
    }

    function report(before, video, audio, method) {
        var sequence = U.activeSequence();
        return {
            added: {
                video: sequence.videoTracks.numTracks - before.video,
                audio: sequence.audioTracks.numTracks - before.audio
            },
            requested: { video: video, audio: audio },
            video_tracks: sequence.videoTracks.numTracks,
            audio_tracks: sequence.audioTracks.numTracks,
            method: method
        };
    }

    function setTrack(params) {
        var sequence = U.activeSequence();
        var track = getTrack(sequence, params.track);
        var applied = {};
        var unsupportedFields = [];

        if (params.name !== undefined) {
            try { track.name = String(params.name); applied.name = params.name; }
            catch (e) { unsupportedFields[unsupportedFields.length] = 'name'; }
        }
        if (params.muted !== undefined) {
            try { track.setMute(params.muted ? 1 : 0); applied.muted = !!params.muted; }
            catch (e) { unsupportedFields[unsupportedFields.length] = 'muted'; }
        }
        if (params.locked !== undefined) {
            try { track.setLocked(params.locked ? 1 : 0); applied.locked = !!params.locked; }
            catch (e) { unsupportedFields[unsupportedFields.length] = 'locked'; }
        }
        if (params.targeted !== undefined) {
            try {
                track.setTargeted(params.targeted ? 1 : 0, true);
                applied.targeted = !!params.targeted;
            } catch (e) { unsupportedFields[unsupportedFields.length] = 'targeted'; }
        }
        if (params.solo !== undefined) {
            // Solo has no scripting entry point on any shipping build; say so
            // rather than reporting a change that did not happen.
            unsupportedFields[unsupportedFields.length] = 'solo';
        }

        var result = { applied: applied };
        if (unsupportedFields.length) {
            result.unsupported = unsupportedFields;
            result.note = 'These properties are not settable by script on this '
                        + 'Premiere build: ' + unsupportedFields.join(', ');
        }
        return result;
    }

    function removeTrack(params) {
        var sequence = U.activeSequence();
        if (params.empty_only) {
            var removed = 0;
            var kinds = ['video', 'audio'];
            for (var k = 0; k < kinds.length; k++) {
                var tracks = trackCollection(sequence, kinds[k]);
                for (var t = tracks.numTracks - 1; t >= 0; t--) {
                    if (tracks[t].clips.numItems === 0) {
                        try {
                            if (kinds[k] === 'video') {
                                sequence.deleteTracks(0, 0, 1, t);
                            } else {
                                sequence.deleteTracks(0, 0, 0, 0, 1, t);
                            }
                            removed++;
                        } catch (e) { /* leave it in place */ }
                    }
                }
            }
            if (!removed) {
                return {
                    removed: 0,
                    note: 'No empty tracks were removed. Either there were none, '
                        + 'or this Premiere build does not expose track deletion '
                        + 'to scripting -- empty tracks are harmless either way.'
                };
            }
            return { removed: removed };
        }

        throw U.unsupported(
            'Removing a specific track is not exposed by the Premiere scripting API',
            'track.remove with empty_only=true removes empty tracks; otherwise '
                + 'delete the track by hand in Premiere'
        );
    }

    return {
        parseTrack: parseTrack,
        getTrack: getTrack,
        trackLabel: trackLabel,
        clipId: clipId,
        describeClip: describeClip,
        resolve: resolve,
        resolveOne: resolveOne,
        snapshot: snapshot,
        revisionOf: revisionOf,
        playhead: playhead,
        addTracks: addTracks,
        setTrack: setTrack,
        removeTrack: removeTrack
    };
}());
