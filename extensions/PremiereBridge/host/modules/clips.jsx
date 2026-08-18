/**
 * Clip arrangement: insert, overwrite, append, remove, move, split, trim,
 * slip, duplicate, speed, freeze, gaps.
 *
 * Premiere's DOM covers placement and removal well but has real holes -- there
 * is no razor, no ripple trim and no speed setter on the documented TrackItem
 * API. Those go through the QE DOM, which is undocumented but is the only
 * mechanism that exists. Every QE call here is guarded and reports precisely
 * what failed, so a build that changes QE surfaces as a clear error rather
 * than a mysteriously unchanged timeline.
 */

var NovaClips = (function () {
    var U = NovaUtil;
    var T = NovaTimeline;

    // ------------------------------------------------------------------
    // Placement
    // ------------------------------------------------------------------

    function place(params, mode) {
        var sequence = U.activeSequence();
        var asset = NovaProject.findAsset(params.asset);
        var trackSpec = params.track || { type: 'video', index: 0 };
        var track = T.getTrack(sequence, trackSpec);

        // Source in/out are set on the project item before placement -- that is
        // how Premiere decides which part of the media lands on the timeline.
        if (params['in'] !== undefined || params.out !== undefined) {
            try {
                if (params['in'] !== undefined) {
                    asset.setInPoint(U.ticksOf(params['in']), 4);
                }
                if (params.out !== undefined) {
                    asset.setOutPoint(U.ticksOf(params.out), 4);
                }
            } catch (e) {
                throw U.fail('Could not set source in/out on "' + asset.name +
                             '": ' + e);
            }
        }

        var at = Number(params.time || 0);
        if (mode === 'append') {
            at = trackEnd(track) + Number(params.gap || 0);
        }

        var before = track.clips.numItems;
        try {
            if (mode === 'insert') {
                track.insertClip(asset, U.ticksOf(at));
            } else {
                track.overwriteClip(asset, U.ticksOf(at));
            }
        } catch (e2) {
            throw U.fail('Could not place "' + asset.name + '" on ' +
                         T.trackLabel(trackSpec.type || 'video',
                                      trackSpec.index || 0) + ': ' + e2, {
                hint: 'The track may be locked, or the asset type may not be '
                    + 'valid for it (video on an audio track, for example).'
            });
        }

        if (track.clips.numItems === before) {
            throw U.fail('Premiere accepted the placement but no clip appeared', {
                hint: 'This usually means the track is locked or targeted off.'
            });
        }

        var placed = findClipAt(track, at);
        return {
            placed: String(asset.name),
            track: T.trackLabel(trackSpec.type || 'video', trackSpec.index || 0),
            time: at,
            clip: placed ? T.describeClip(placed.clip,
                                          trackSpec.type || 'video',
                                          trackSpec.index || 0,
                                          placed.index, {}) : null
        };
    }

    function trackEnd(track) {
        var end = 0;
        for (var i = 0; i < track.clips.numItems; i++) {
            var candidate = U.secondsOf(track.clips[i].end);
            if (candidate > end) { end = candidate; }
        }
        return end;
    }

    function findClipAt(track, seconds) {
        for (var i = 0; i < track.clips.numItems; i++) {
            var clip = track.clips[i];
            if (U.secondsOf(clip.start) <= seconds + 0.001 &&
                U.secondsOf(clip.end) > seconds + 0.001) {
                return { clip: clip, index: i };
            }
        }
        return null;
    }

    function insert(params) { return place(params, 'insert'); }
    function overwrite(params) { return place(params, 'overwrite'); }
    function append(params) { return place(params, 'append'); }

    // ------------------------------------------------------------------
    // Removal and movement
    // ------------------------------------------------------------------

    function remove(params) {
        var matches = T.resolve(params.clip);
        var ripple = !!params.ripple;
        var removed = 0;

        // Remove back-to-front: removing a clip renumbers everything after it,
        // so forward iteration would delete the wrong clips.
        for (var i = matches.length - 1; i >= 0; i--) {
            try {
                matches[i].clip.remove(ripple, true);
                removed++;
            } catch (e) {
                throw U.fail('Could not remove clip "' + matches[i].clip.name +
                             '": ' + e, {
                    detail: { removed_before_failure: removed },
                    hint: 'The track may be locked.'
                });
            }
        }
        return { removed: removed, ripple: ripple };
    }

    function move(params) {
        var matches = T.resolve(params.clip);
        var moved = [];

        for (var i = 0; i < matches.length; i++) {
            var clip = matches[i].clip;
            var currentStart = U.secondsOf(clip.start);
            var delta = 0;

            if (params.to !== undefined) {
                delta = Number(params.to) - currentStart;
            } else if (params.by !== undefined) {
                delta = Number(params.by);
            }

            if (params.track) {
                moveToTrack(matches[i], params.track,
                            params.to !== undefined ? Number(params.to)
                                                    : currentStart + delta);
                moved[moved.length] = {
                    clip: String(clip.name),
                    moved_to_track: T.trackLabel(params.track.type,
                                                 params.track.index)
                };
                continue;
            }

            if (delta !== 0) {
                try {
                    // TrackItem.move takes a RELATIVE offset.
                    clip.move(U.timeFromSeconds(delta));
                } catch (e) {
                    throw U.fail('Could not move "' + clip.name + '": ' + e, {
                        hint: 'Something may already occupy the destination, or '
                            + 'the track is locked.'
                    });
                }
            }
            moved[moved.length] = {
                clip: String(clip.name),
                from: currentStart,
                to: currentStart + delta
            };
        }
        return { moved: moved.length, clips: moved };
    }

    /**
     * Cross-track move.
     *
     * There is no "change track" API. The working approach is to place the
     * same project item (with the same source in/out) on the destination track
     * and remove the original -- which preserves the media and timing but not
     * effects/keyframes. That loss is reported rather than hidden, because
     * silently dropping a grade during a move would be a nasty surprise.
     */
    function moveToTrack(match, trackSpec, at) {
        var sequence = U.activeSequence();
        var destination = T.getTrack(sequence, trackSpec);
        var clip = match.clip;
        var item = clip.projectItem;
        if (!item) {
            throw U.unsupported(
                'This clip has no project item, so it cannot be moved between tracks',
                'move it by hand, or rebuild it on the destination track'
            );
        }

        var inPoint = U.secondsOf(clip.inPoint);
        var outPoint = U.secondsOf(clip.outPoint);
        try {
            item.setInPoint(U.ticksOf(inPoint), 4);
            item.setOutPoint(U.ticksOf(outPoint), 4);
            destination.overwriteClip(item, U.ticksOf(at));
            clip.remove(false, true);
        } catch (e) {
            throw U.fail('Cross-track move failed: ' + e);
        }
    }

    function duplicate(params) {
        var match = T.resolveOne(params.clip);
        var clip = match.clip;
        var item = clip.projectItem;
        if (!item) {
            throw U.unsupported(
                'This clip cannot be duplicated (it has no project item)',
                'copy and paste it by hand in Premiere'
            );
        }
        var sequence = U.activeSequence();
        var trackSpec = params.track ||
            { type: match.type, index: match.trackIndex };
        var destination = T.getTrack(sequence, trackSpec);
        var at = Number(params.time);

        try {
            item.setInPoint(U.ticksOf(U.secondsOf(clip.inPoint)), 4);
            item.setOutPoint(U.ticksOf(U.secondsOf(clip.outPoint)), 4);
            if (params.mode === 'insert') {
                destination.insertClip(item, U.ticksOf(at));
            } else {
                destination.overwriteClip(item, U.ticksOf(at));
            }
        } catch (e) {
            throw U.fail('Could not duplicate "' + clip.name + '": ' + e);
        }

        return {
            duplicated: String(clip.name),
            time: at,
            track: T.trackLabel(trackSpec.type, trackSpec.index),
            note: 'The copy carries the same media and timing. Effects and '
                + 'keyframes are not copied -- Premiere exposes no scripted '
                + 'effect-copy; reapply them with effect.apply / keyframe.set.'
        };
    }

    // ------------------------------------------------------------------
    // Cutting and trimming
    // ------------------------------------------------------------------

    function split(params) {
        var sequence = U.activeSequence();
        var fps = U.sequenceFps(sequence);
        var times = params.at;
        var cuts = 0;
        var failures = [];

        // Cut back-to-front so earlier cuts do not renumber later ones.
        var ordered = [];
        for (var i = 0; i < times.length; i++) { ordered[i] = Number(times[i]); }
        ordered.sort(function (a, b) { return b - a; });

        var qeSeq = U.qeSequence();

        for (var t = 0; t < ordered.length; t++) {
            var at = ordered[t];
            var tracks = tracksToCut(params, sequence);
            for (var k = 0; k < tracks.length; k++) {
                var spec = tracks[k];
                try {
                    var qeTrack = (spec.type === 'audio')
                        ? qeSeq.getAudioTrackAt(spec.index)
                        : qeSeq.getVideoTrackAt(spec.index);
                    if (!qeTrack) { continue; }
                    qeTrack.razor(U.timecode(at, fps));
                    cuts++;
                } catch (e) {
                    failures[failures.length] = {
                        track: T.trackLabel(spec.type, spec.index),
                        at: at, error: String(e)
                    };
                }
            }
        }

        if (!cuts) {
            throw U.unsupported(
                'Could not razor the timeline through the QE DOM',
                'split the clip by hand, or use clip.trim to shorten it instead',
                { failures: failures }
            );
        }
        var result = { cuts: cuts };
        if (failures.length) { result.failed = failures; }
        return result;
    }

    function tracksToCut(params, sequence) {
        var out = [];
        if (params.all_tracks) {
            for (var v = 0; v < sequence.videoTracks.numTracks; v++) {
                out[out.length] = { type: 'video', index: v };
            }
            for (var a = 0; a < sequence.audioTracks.numTracks; a++) {
                out[out.length] = { type: 'audio', index: a };
            }
            return out;
        }
        var matches = T.resolve(params.clip, sequence);
        var seen = {};
        for (var i = 0; i < matches.length; i++) {
            var key = matches[i].type + matches[i].trackIndex;
            if (!seen[key]) {
                seen[key] = true;
                out[out.length] = {
                    type: matches[i].type, index: matches[i].trackIndex
                };
            }
        }
        return out;
    }

    /**
     * Trim a clip edge.
     *
     * Non-ripple trims are done on the DOM by setting start/end, which is
     * reliable. Ripple trims need the neighbours to move too; that is done by
     * trimming and then shifting every later clip on the same track, because
     * Premiere exposes no ripple-trim call.
     */
    function trim(params) {
        var matches = T.resolve(params.clip);
        var results = [];

        for (var i = 0; i < matches.length; i++) {
            var clip = matches[i].clip;
            var start = U.secondsOf(clip.start);
            var end = U.secondsOf(clip.end);
            var edge = params.edge;
            var newStart = start;
            var newEnd = end;

            if (edge === 'in' || edge === 'both') {
                newStart = (params.to !== undefined)
                    ? Number(params.to)
                    : start + Number(params.by || 0);
            }
            if (edge === 'out' || edge === 'both') {
                newEnd = (params.to !== undefined)
                    ? Number(params.to)
                    : end - Number(params.by || 0);
            }

            if (newEnd <= newStart) {
                throw U.fail(
                    'That trim would leave "' + clip.name +
                    '" with no duration (' + newStart.toFixed(3) + 's to ' +
                    newEnd.toFixed(3) + 's)',
                    { hint: 'Trim less, or remove the clip instead' }
                );
            }

            var headDelta = newStart - start;
            var tailDelta = newEnd - end;

            try {
                // Move the in-point in source AND sequence together, otherwise
                // the clip slips instead of trimming.
                if (headDelta !== 0) {
                    clip.inPoint = U.timeFromSeconds(
                        U.secondsOf(clip.inPoint) + headDelta);
                    clip.start = U.timeFromSeconds(newStart);
                }
                if (tailDelta !== 0) {
                    clip.outPoint = U.timeFromSeconds(
                        U.secondsOf(clip.outPoint) + tailDelta);
                    clip.end = U.timeFromSeconds(newEnd);
                }
            } catch (e) {
                throw U.fail('Could not trim "' + clip.name + '": ' + e, {
                    hint: 'Trimming past the available source media is the usual '
                        + 'cause -- there may be no more handle on that side.'
                });
            }

            if (params.ripple) {
                var shift = (edge === 'in') ? -headDelta : tailDelta;
                rippleFrom(matches[i], newEnd, shift);
            }

            results[results.length] = {
                clip: String(clip.name), start: newStart, end: newEnd
            };
        }
        return { trimmed: results.length, clips: results };
    }

    function rippleFrom(match, afterTime, shift) {
        if (!shift) { return 0; }
        var sequence = U.activeSequence();
        var track = T.getTrack(sequence,
                               { type: match.type, index: match.trackIndex });
        var moved = 0;
        // Later clips first when shifting right, so they never collide with a
        // clip that has not moved yet.
        var indices = [];
        for (var i = 0; i < track.clips.numItems; i++) { indices[i] = i; }
        if (shift > 0) { indices.reverse(); }

        for (var k = 0; k < indices.length; k++) {
            var clip = track.clips[indices[k]];
            if (U.secondsOf(clip.start) < afterTime - 0.0005) { continue; }
            try { clip.move(U.timeFromSeconds(shift)); moved++; }
            catch (e) { /* blocked; leave it */ }
        }
        return moved;
    }

    function slip(params) {
        var matches = T.resolve(params.clip);
        var slipped = 0;
        for (var i = 0; i < matches.length; i++) {
            var clip = matches[i].clip;
            var by = Number(params.by);
            try {
                clip.inPoint = U.timeFromSeconds(U.secondsOf(clip.inPoint) + by);
                clip.outPoint = U.timeFromSeconds(U.secondsOf(clip.outPoint) + by);
                slipped++;
            } catch (e) {
                throw U.fail('Could not slip "' + clip.name + '": ' + e, {
                    hint: 'There may not be enough source media on that side.'
                });
            }
        }
        return { slipped: slipped };
    }

    function gapRemove(params) {
        var sequence = U.activeSequence();
        var track = T.getTrack(sequence, params.track);
        var closed = 0;
        var guard = 0;

        // Repeat until stable: closing one gap can reveal the next.
        for (;;) {
            var gap = nextGap(track, params.at);
            if (!gap || guard++ > 200) { break; }
            var moved = 0;
            for (var i = track.clips.numItems - 1; i >= 0; i--) {
                var clip = track.clips[i];
                if (U.secondsOf(clip.start) >= gap.end - 0.0005) {
                    try { clip.move(U.timeFromSeconds(-gap.length)); moved++; }
                    catch (e) { /* blocked */ }
                }
            }
            if (!moved) { break; }
            closed++;
            if (params.at !== undefined) { break; }
        }
        return { gaps_closed: closed };
    }

    function nextGap(track, at) {
        var previousEnd = 0;
        for (var i = 0; i < track.clips.numItems; i++) {
            var clip = track.clips[i];
            var start = U.secondsOf(clip.start);
            if (start - previousEnd > 0.001) {
                var gap = { start: previousEnd, end: start,
                            length: start - previousEnd };
                if (at === undefined ||
                    (Number(at) >= gap.start && Number(at) <= gap.end)) {
                    return gap;
                }
            }
            previousEnd = U.secondsOf(clip.end);
        }
        return null;
    }

    // ------------------------------------------------------------------
    // Flags, speed, freeze
    // ------------------------------------------------------------------

    function setFlags(params) {
        var matches = T.resolve(params.clip);
        var applied = [];
        var unsupportedFields = {};

        for (var i = 0; i < matches.length; i++) {
            var clip = matches[i].clip;
            var entry = { clip: String(clip.name) };
            if (params.name !== undefined) {
                try { clip.name = String(params.name); entry.name = params.name; }
                catch (e) { unsupportedFields.name = true; }
            }
            if (params.enabled !== undefined) {
                try {
                    clip.disabled = !params.enabled;
                    entry.enabled = !!params.enabled;
                } catch (e2) { unsupportedFields.enabled = true; }
            }
            if (params.label !== undefined) {
                // Label colour is not exposed on TrackItem in any shipping build.
                unsupportedFields.label = true;
            }
            applied[applied.length] = entry;
        }

        var result = { updated: applied.length, clips: applied };
        var missing = [];
        for (var key in unsupportedFields) {
            if (Object.prototype.hasOwnProperty.call(unsupportedFields, key)) {
                missing[missing.length] = key;
            }
        }
        if (missing.length) {
            result.unsupported = missing;
            result.note = 'Not settable by script on this build: ' + missing.join(', ');
        }
        return result;
    }

    function speed(params) {
        var matches = T.resolve(params.clip);
        var sequence = U.activeSequence();
        var fps = U.sequenceFps(sequence);
        var qeSeq = U.qeSequence();
        var rate = (params.rate === undefined) ? 1 : Number(params.rate);
        var reverse = !!params.reverse;
        var maintainPitch = U.defined(params.maintain_pitch, true);
        var changed = [];

        for (var i = 0; i < matches.length; i++) {
            var match = matches[i];
            var qeTrack = (match.type === 'audio')
                ? qeSeq.getAudioTrackAt(match.trackIndex)
                : qeSeq.getVideoTrackAt(match.trackIndex);
            if (!qeTrack) {
                throw U.fail('QE could not see track ' +
                             T.trackLabel(match.type, match.trackIndex));
            }

            var qeClip = qeItemFor(qeTrack, match.clip);
            if (!qeClip) {
                throw U.fail('QE could not find "' + match.clip.name +
                             '" on ' + T.trackLabel(match.type, match.trackIndex), {
                    hint: 'Transitions between clips can shift QE indices. '
                        + 'Re-read timeline.snapshot and retry.'
                });
            }

            var sourceDuration = U.secondsOf(match.clip.duration);
            var newDuration = (rate > 0) ? (sourceDuration / rate) : sourceDuration;
            try {
                qeClip.setSpeed(rate, U.timecode(newDuration, fps),
                                reverse, maintainPitch, !!params.ripple);
                changed[changed.length] = {
                    clip: String(match.clip.name),
                    rate: rate,
                    reverse: reverse,
                    duration: newDuration
                };
            } catch (e) {
                throw U.fail('Could not set speed on "' + match.clip.name +
                             '": ' + e, {
                    hint: 'Speed changes go through the undocumented QE DOM. '
                        + 'For variable speed use clip.speed_ramp, which uses '
                        + 'keyframable Time Remapping instead.'
                });
            }
        }
        return { changed: changed.length, clips: changed };
    }

    function qeItemFor(qeTrack, domClip) {
        var wantedStart = U.secondsOf(domClip.start);
        var count = 0;
        try { count = qeTrack.numItems; } catch (e) { return null; }

        for (var i = 0; i < count; i++) {
            var item;
            try { item = qeTrack.getItemAt(i); } catch (e2) { continue; }
            if (!item) { continue; }
            // QE reports timecode strings; matching on name plus start second
            // is the most robust identification available.
            var matchesName = false;
            try { matchesName = U.nameMatches(item.name, domClip.name); }
            catch (e3) { /* some QE items have no name */ }
            var startsHere = false;
            try {
                startsHere = Math.abs(qeSeconds(item.start) - wantedStart) < 0.05;
            } catch (e4) { /* fall back to name only */ }
            if (startsHere || (matchesName && count === 1)) { return item; }
        }
        return null;
    }

    function qeSeconds(value) {
        // QE times arrive as either a timecode string or an object with .secs.
        if (value === null || value === undefined) { return -1; }
        try {
            if (value.secs !== undefined) { return Number(value.secs); }
        } catch (e) { /* not that shape */ }
        var text = String(value);
        var parts = text.split(/[:;]/);
        if (parts.length === 4) {
            return Number(parts[0]) * 3600 + Number(parts[1]) * 60 +
                   Number(parts[2]);
        }
        var asNumber = Number(text);
        return isNaN(asNumber) ? -1 : asNumber;
    }

    /**
     * Freeze frame.
     *
     * Premiere's "Frame Hold" is not scriptable. The equivalent that IS
     * scriptable is a Time Remapping curve held at a constant source time,
     * which produces the same result on export.
     */
    function freeze(params) {
        var match = T.resolveOne(params.clip);
        var clip = match.clip;
        var at = (params.at !== undefined)
            ? Number(params.at) : 0;

        var component;
        try {
            component = NovaParams.findComponent(clip, 'Time Remapping');
        } catch (e) {
            throw U.unsupported(
                'This clip has no Time Remapping component, so it cannot be '
                    + 'frozen by script',
                'apply a Frame Hold by hand in Premiere, or export a still and '
                    + 'place it with graphic.image',
                { clip: String(clip.name) }
            );
        }

        var param;
        try {
            param = NovaParams.findParam(component, 'Speed');
        } catch (e2) {
            throw U.unsupported(
                'Time Remapping > Speed is not exposed on this clip',
                'apply a Frame Hold by hand in Premiere'
            );
        }

        var duration = (params.duration !== undefined)
            ? Number(params.duration) : U.secondsOf(clip.duration) - at;

        try {
            if (!param.isTimeVarying()) { param.setTimeVarying(true); }
            var startTime = U.timeFromSeconds(
                NovaParams.paramTime(clip, at, 'clip'));
            var endTime = U.timeFromSeconds(
                NovaParams.paramTime(clip, at + duration, 'clip'));
            param.addKey(startTime);
            param.setValueAtKey(startTime, 0, true);
            param.addKey(endTime);
            param.setValueAtKey(endTime, 0, true);
        } catch (e3) {
            throw U.fail('Could not write the freeze: ' + e3);
        }

        return {
            frozen: String(clip.name),
            at: at,
            duration: duration,
            method: 'time-remap hold',
            note: 'Premiere exposes no scriptable Frame Hold; this holds the '
                + 'frame with a zero-speed Time Remapping segment.'
        };
    }

    function link(params) {
        throw U.unsupported(
            'Linking and unlinking audio/video is not exposed by the Premiere '
                + 'scripting API',
            'select the clips in Premiere and use Clip > Link/Unlink, or operate '
                + 'on the video and audio clips separately (every clip operation '
                + 'here accepts an audio track selector)'
        );
    }

    function resolveOp(params) {
        var matches = T.resolve(params.clip);
        var out = [];
        for (var i = 0; i < matches.length; i++) {
            out[out.length] = T.describeClip(
                matches[i].clip, matches[i].type,
                matches[i].trackIndex, matches[i].clipIndex, {});
        }
        return out;
    }

    /** Swap a clip's media for another file, keeping its timing in place. */
    function replaceSource(params) {
        var match = T.resolveOne(params.clip);
        var clip = match.clip;
        if (!File(params.path).exists) {
            throw U.fail('File not found: ' + params.path);
        }

        var sequence = U.activeSequence();
        var start = U.secondsOf(clip.start);
        var duration = U.secondsOf(clip.duration);
        var track = T.getTrack(sequence,
                               { type: match.type, index: match.trackIndex });

        var imported = NovaProject.importFiles({
            paths: [params.path], bin: 'Nova Generated'
        });
        var asset = NovaProject.findAsset(
            imported.names.length ? imported.names[0] : params.path);

        try {
            clip.remove(false, true);
            asset.setInPoint(U.ticksOf(0), 4);
            asset.setOutPoint(U.ticksOf(duration), 4);
            track.overwriteClip(asset, U.ticksOf(start));
        } catch (e) {
            throw U.fail('Could not replace the clip source: ' + e);
        }

        var placed = findClipAt(track, start);
        if (placed && params.name) {
            try { placed.clip.name = String(params.name); } catch (e2) { /* fine */ }
        }
        return {
            replaced: true, time: start, duration: duration,
            note: 'The clip was rebuilt from new media; effects and keyframes '
                + 'on the previous clip were not carried over.'
        };
    }

    return {
        insert: insert,
        overwrite: overwrite,
        append: append,
        remove: remove,
        move: move,
        duplicate: duplicate,
        split: split,
        trim: trim,
        slip: slip,
        gapRemove: gapRemove,
        setFlags: setFlags,
        speed: speed,
        freeze: freeze,
        link: link,
        resolveOp: resolveOp,
        replaceSource: replaceSource,
        findClipAt: findClipAt,
        trackEnd: trackEnd,
        qeItemFor: qeItemFor
    };
}());
