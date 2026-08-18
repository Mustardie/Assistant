/**
 * Overlays, motion-graphics templates, captions and adjustment layers.
 *
 * `overlay.place` is the workhorse behind text.create, graphic.shape,
 * graphic.image and graphic-mode captions: it imports a generated PNG, drops it
 * on a track, sets its duration, and applies position/scale/rotation/opacity in
 * one call so the clip is never briefly visible in the wrong place.
 */

var NovaMedia = (function () {
    var U = NovaUtil;
    var T = NovaTimeline;

    var GENERATED_BIN = 'Nova Generated';

    // ------------------------------------------------------------------
    // Overlays
    // ------------------------------------------------------------------

    function place(params) {
        var sequence = U.activeSequence();
        if (!File(params.path).exists) {
            throw U.fail('Overlay file not found: ' + params.path);
        }

        var asset = importOnce(params.path);
        var trackSpec = params.track || pickOverlayTrack(sequence, params.time,
                                                         params.duration);
        var track = T.getTrack(sequence, trackSpec);
        var at = Number(params.time || 0);
        var duration = Number(params.duration || 4);

        // A still's timeline length comes from its source in/out, so set those
        // before placing rather than trimming afterwards.
        try {
            asset.setInPoint(U.ticksOf(0), 4);
            asset.setOutPoint(U.ticksOf(duration), 4);
        } catch (e) { /* some stills reject in/out; length is fixed below */ }

        try {
            track.overwriteClip(asset, U.ticksOf(at));
        } catch (e2) {
            throw U.fail('Could not place the overlay on ' +
                         T.trackLabel(trackSpec.type, trackSpec.index) +
                         ': ' + e2, {
                hint: 'The track may be locked. Overlays need an unlocked video '
                    + 'track above the footage.'
            });
        }

        var placed = NovaClips.findClipAt(track, at);
        if (!placed) {
            throw U.fail('The overlay was placed but could not be found again', {
                hint: 'Another clip may already occupy that time on this track.'
            });
        }

        // Enforce the requested duration; stills sometimes land at the default
        // still duration regardless of the in/out we set.
        try {
            var actual = U.secondsOf(placed.clip.duration);
            if (Math.abs(actual - duration) > 0.01) {
                placed.clip.end = U.timeFromSeconds(at + duration);
            }
        } catch (e3) { /* keep whatever length it got */ }

        if (params.name) {
            try { placed.clip.name = String(params.name); } catch (e4) { /* fine */ }
        }

        var result = {
            placed: String(params.name || asset.name),
            clip_id: T.clipId(placed.clip, trackSpec.type, trackSpec.index,
                              placed.index),
            track: T.trackLabel(trackSpec.type, trackSpec.index),
            time: at,
            duration: duration,
            kind: params.kind || 'image'
        };

        if (params.transform) {
            result.transform = applyTransform(placed.clip, params.transform);
        }
        return result;
    }

    /**
     * Import a generated asset once.
     *
     * Text and shape PNGs are content-addressed, so the same overlay used
     * twenty times in a video is one file. Re-importing it each time would
     * litter the project panel with duplicates.
     */
    function importOnce(path) {
        try {
            return NovaProject.findAsset(path);
        } catch (e) {
            NovaProject.importFiles({ paths: [path], bin: GENERATED_BIN });
            return NovaProject.findAsset(path);
        }
    }

    /**
     * Choose a video track for an overlay: the lowest one that is free for the
     * whole window, else a new one on top. Stacking overlays blindly on V2
     * would silently overwrite earlier titles.
     */
    function pickOverlayTrack(sequence, time, duration) {
        var at = Number(time || 0);
        var until = at + Number(duration || 4);

        for (var t = 1; t < sequence.videoTracks.numTracks; t++) {
            var track = sequence.videoTracks[t];
            var free = true;
            for (var c = 0; c < track.clips.numItems; c++) {
                var clip = track.clips[c];
                if (U.secondsOf(clip.end) > at + 0.001 &&
                    U.secondsOf(clip.start) < until - 0.001) {
                    free = false;
                    break;
                }
            }
            if (free) { return { type: 'video', index: t }; }
        }

        try {
            var before = sequence.videoTracks.numTracks;
            sequence.addTracks(1, before, 0, 0, 1, 0, 0);
            if (sequence.videoTracks.numTracks > before) {
                return { type: 'video', index: before };
            }
        } catch (e) { /* fall through */ }

        return { type: 'video', index: sequence.videoTracks.numTracks - 1 };
    }

    function applyTransform(clip, transform) {
        var applied = {};
        var motion = null;
        try { motion = NovaParams.findComponent(clip, 'Motion', 0); }
        catch (e) { return { note: 'This clip has no Motion component.' }; }

        if (transform.position) {
            applied.position = trySet(motion, 'Position', transform.position);
        }
        if (transform.scale !== undefined) {
            applied.scale = trySet(motion, 'Scale', transform.scale);
        }
        if (transform.rotation !== undefined) {
            applied.rotation = trySet(motion, 'Rotation', transform.rotation);
        }
        if (transform.opacity !== undefined) {
            try {
                var opacity = NovaParams.findComponent(clip, 'Opacity', 0);
                applied.opacity = trySet(opacity, 'Opacity', transform.opacity);
            } catch (e2) { applied.opacity = false; }
        }
        return applied;
    }

    function trySet(component, name, value) {
        try {
            NovaParams.findParam(component, name).setValue(value, true);
            return true;
        } catch (e) {
            return false;
        }
    }

    // ------------------------------------------------------------------
    // Motion Graphics templates
    // ------------------------------------------------------------------

    function importMGT(params) {
        var sequence = U.activeSequence();
        if (!U.has(sequence, 'importMGT')) {
            throw U.unsupported(
                'This Premiere build does not expose Sequence.importMGT',
                "use text.create with engine='render' for a rendered overlay"
            );
        }
        if (!File(params.path).exists) {
            throw U.fail('Template not found: ' + params.path);
        }

        var trackSpec = params.track ||
            pickOverlayTrack(sequence, params.time, params.duration || 5);
        var at = U.ticksOf(Number(params.time || 0));

        var item;
        try {
            item = sequence.importMGT(String(params.path), at,
                                      Number(trackSpec.index), 0);
        } catch (e) {
            throw U.fail('Could not import the template: ' + e);
        }
        if (!item) {
            throw U.fail('Premiere returned no clip for that template', {
                hint: 'The .mogrt may target a different Premiere version.'
            });
        }

        if (params.duration) {
            try {
                item.end = U.timeFromSeconds(Number(params.time || 0) +
                                             Number(params.duration));
            } catch (e2) { /* keep the template's own length */ }
        }

        var result = {
            placed: String(item.name),
            track: T.trackLabel('video', trackSpec.index),
            time: Number(params.time || 0)
        };

        if (params.params) {
            result.params = setMGTParams(item, params.params);
        }
        return result;
    }

    function mgtComponent(item) {
        if (!U.has(item, 'getMGTComponent')) {
            throw U.unsupported(
                'This clip exposes no Motion Graphics component',
                'check that the clip really is a .mogrt (mogrt.params lists what '
                    + 'is editable)'
            );
        }
        var component = item.getMGTComponent();
        if (!component) {
            throw U.fail('This clip has no editable template parameters');
        }
        return component;
    }

    function setMGTParams(item, values) {
        var component = mgtComponent(item);
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
            out.available = NovaEffects.paramNames(component);
        }
        return out;
    }

    function listMGTParams(params) {
        var match = T.resolveOne(params.clip);
        var component = mgtComponent(match.clip);
        var out = [];
        for (var i = 0; i < component.properties.numItems; i++) {
            var param = component.properties[i];
            var entry = { name: String(param.displayName) };
            try { entry.value = param.getValue(); } catch (e) { /* opaque */ }
            entry.keyframable = NovaParams.safeCall(param, 'areKeyframesSupported',
                                                    false);
            out[out.length] = entry;
        }
        return { clip: String(match.clip.name), count: out.length, parameters: out };
    }

    // ------------------------------------------------------------------
    // Captions
    // ------------------------------------------------------------------

    function importCaptions(params) {
        var sequence = U.activeSequence();
        if (!File(params.path).exists) {
            throw U.fail('Subtitle file not found: ' + params.path);
        }
        if (!U.has(sequence, 'createCaptionTrack')) {
            throw U.unsupported(
                'This Premiere build does not expose Sequence.createCaptionTrack',
                "use captions.build with mode='graphic' for styled subtitle "
                    + 'overlays'
            );
        }

        NovaProject.importFiles({ paths: [params.path], bin: GENERATED_BIN });
        var asset = NovaProject.findAsset(params.path);

        try {
            sequence.createCaptionTrack(asset, U.ticksOf(Number(params.time || 0)));
        } catch (e) {
            throw U.unsupported(
                'Premiere rejected the caption track: ' + e,
                "use captions.build with mode='graphic' instead"
            );
        }
        return {
            captions: String(asset.name),
            time: Number(params.time || 0),
            note: 'Caption track created. Styling of native captions is not '
                + "script-controllable; use mode='graphic' when you need "
                + 'typography control.'
        };
    }

    // ------------------------------------------------------------------
    // Adjustment layers
    // ------------------------------------------------------------------

    /**
     * Premiere has no scripting call that creates an adjustment layer.
     *
     * If the user has one in the project already (the usual case in a working
     * project), it is placed like any other asset -- which is a genuine
     * adjustment layer with genuine pass-through behaviour. If not, we report
     * a fallback rather than substituting a transparent-video clip, because
     * transparent video does NOT affect the tracks beneath it and pretending
     * otherwise would produce a grade that silently does nothing.
     */
    function createAdjustment(params) {
        var sequence = U.activeSequence();
        var existing = null;
        try { existing = NovaProject.findAsset('Adjustment Layer'); }
        catch (e) { existing = null; }

        if (!existing) {
            return {
                created: false,
                fallback: 'per_clip',
                note: 'No adjustment layer exists in this project and Premiere '
                    + 'cannot create one by script. Create one once (Project '
                    + 'panel > New Item > Adjustment Layer) and this operation '
                    + 'will use it; until then, effects are applied per clip.'
            };
        }

        var trackSpec = params.track ||
            pickOverlayTrack(sequence, params.time, params.duration);
        var track = T.getTrack(sequence, trackSpec);
        var at = Number(params.time || 0);
        var duration = Number(params.duration);

        try {
            existing.setInPoint(U.ticksOf(0), 4);
            existing.setOutPoint(U.ticksOf(duration), 4);
            track.overwriteClip(existing, U.ticksOf(at));
        } catch (e2) {
            throw U.fail('Could not place the adjustment layer: ' + e2);
        }

        var placed = NovaClips.findClipAt(track, at);
        if (placed) {
            try { placed.clip.end = U.timeFromSeconds(at + duration); }
            catch (e3) { /* keep what we got */ }
        }

        return {
            created: true,
            clip_id: placed ? T.clipId(placed.clip, trackSpec.type,
                                       trackSpec.index, placed.index) : null,
            track: T.trackLabel(trackSpec.type, trackSpec.index),
            time: at,
            duration: duration
        };
    }

    // ------------------------------------------------------------------
    // Frame export
    // ------------------------------------------------------------------

    /**
     * Export a still of the program monitor at a given time.
     *
     * This is what lets a *visual* editing model work: it can look at the
     * frame it is about to cut on rather than reasoning blind from clip names
     * and timings.
     *
     * The call that does this has moved around between Premiere versions and
     * is not in the public scripting reference, so rather than pin one shape,
     * try the known ones and report honestly if none exist. The named
     * alternative (pull the frame from the source file directly) is something
     * the assistant can genuinely do outside Premiere, so a negative answer
     * here is still actionable.
     */
    function exportFrame(params) {
        var sequence = U.activeSequence();
        var at = Number(params.time || 0);
        var path = String(params.path);
        var ticks = U.ticksOf(at);

        var attempts = [
            function () { return sequence.exportFramePNG(ticks, path); },
            function () { return sequence.exportFramePNG(at, path); },
            function () { return sequence.exportFrameJPEG(ticks, path); },
            function () {
                var qe = U.qeDom();
                if (!qe || !qe.source) { return false; }
                qe.source.exportFramePNG(U.timecode(at, U.sequenceFps(sequence)),
                                         path);
                return true;
            }
        ];

        var errors = [];
        for (var i = 0; i < attempts.length; i++) {
            try {
                var outcome = attempts[i]();
                if (outcome === false) { continue; }
                if (File(path).exists) {
                    return { exported: path, time: at, method: i };
                }
            } catch (e) {
                errors[errors.length] = String(e.message || e);
            }
        }

        throw U.unsupported(
            'This Premiere build exposes no scriptable frame export',
            'read the frame straight from the source media file instead '
                + '(timeline.snapshot gives each clip its source_path, start '
                + 'and in_point, which is enough to seek to the same frame)',
            { attempts: errors }
        );
    }

    return {
        place: place,
        exportFrame: exportFrame,
        importMGT: importMGT,
        listMGTParams: listMGTParams,
        importCaptions: importCaptions,
        createAdjustment: createAdjustment,
        pickOverlayTrack: pickOverlayTrack
    };
}());
