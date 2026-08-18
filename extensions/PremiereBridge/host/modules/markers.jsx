/**
 * Markers, and copying attributes between clips.
 *
 * Two capabilities that a serious YouTube edit needs and that the rest of the
 * catalog cannot express:
 *
 * 1. **Markers.** Beat-synced cutting starts with knowing where the beats are.
 *    An editor (human or model) drops markers on the music, and the plan then
 *    cuts, times animations and places overlays against them. Chapter markers
 *    are also how YouTube chapters get exported. Premiere's marker API is
 *    documented and stable, unlike much of what this bridge has to use.
 *
 * 2. **Attribute copying.** "Grade this shot, then make the other six match"
 *    is one of the most common real editing actions. Premiere has no scripted
 *    copy/paste-attributes, but effects and their parameters (including
 *    keyframes) can be read off one clip and replayed onto another, which is
 *    the same result by a different route.
 */

var NovaMarkers = (function () {
    var U = NovaUtil;
    var T = NovaTimeline;

    // ------------------------------------------------------------------
    // Markers
    // ------------------------------------------------------------------

    function collectionFor(params) {
        // Markers live either on the sequence (timeline markers) or on a
        // project item (clip markers); both expose the same collection API.
        if (params && params.asset) {
            var asset = NovaProject.findAsset(params.asset);
            if (!U.has(asset, 'getMarkers')) {
                throw U.unsupported(
                    'This project item does not expose markers',
                    'use sequence markers instead (omit "asset")'
                );
            }
            return { markers: asset.getMarkers(), owner: String(asset.name),
                     scope: 'asset' };
        }
        var sequence = U.activeSequence();
        if (!U.has(sequence, 'markers')) {
            throw U.unsupported(
                'This Premiere build does not expose sequence markers',
                'place markers by hand, or drive timings from timeline.snapshot'
            );
        }
        return { markers: sequence.markers, owner: String(sequence.name),
                 scope: 'sequence' };
    }

    function each(collection) {
        /** Markers are a linked list, not an indexable collection. */
        var out = [];
        try {
            var marker = collection.getFirstMarker();
            var guard = 0;
            while (marker && guard++ < 5000) {
                out[out.length] = marker;
                marker = collection.getNextMarker(marker);
            }
        } catch (e) { /* an empty or unreadable collection yields nothing */ }
        return out;
    }

    function describe(marker) {
        var entry = {
            name: safe(function () { return String(marker.name); }, ''),
            comment: safe(function () { return String(marker.comments); }, ''),
            time: U.secondsOf(marker.start),
            type: safe(function () { return String(marker.type); }, 'Comment')
        };
        var end = safe(function () { return U.secondsOf(marker.end); }, null);
        if (end !== null && end > entry.time) {
            entry.end = end;
            entry.duration = end - entry.time;
        }
        return entry;
    }

    function safe(fn, fallback) {
        try {
            var value = fn();
            return (value === undefined || value === null) ? fallback : value;
        } catch (e) { return fallback; }
    }

    function list(params) {
        var target = collectionFor(params);
        var markers = each(target.markers);
        var out = [];
        for (var i = 0; i < markers.length; i++) {
            var entry = describe(markers[i]);
            if (params.type && !U.nameMatches(entry.type, params.type)) { continue; }
            if (params.range &&
                (entry.time < Number(params.range[0]) ||
                 entry.time > Number(params.range[1]))) { continue; }
            out[out.length] = entry;
        }
        out.sort(function (a, b) { return a.time - b.time; });
        return { owner: target.owner, scope: target.scope,
                 count: out.length, markers: out };
    }

    function add(params) {
        var target = collectionFor(params);
        var at = Number(params.time || 0);
        var marker;
        try {
            marker = target.markers.createMarker(at);
        } catch (e) {
            throw U.fail('Could not create a marker at ' + at + 's: ' + e);
        }
        if (!marker) {
            throw U.fail('Premiere returned no marker for ' + at + 's');
        }

        if (params.name !== undefined) {
            try { marker.name = String(params.name); } catch (e2) { /* optional */ }
        }
        if (params.comment !== undefined) {
            try { marker.comments = String(params.comment); }
            catch (e3) { /* optional */ }
        }
        if (params.duration) {
            try {
                marker.end = U.timeFromSeconds(at + Number(params.duration));
            } catch (e4) { /* point marker is fine */ }
        }

        var applied = String(params.type || 'comment').toLowerCase();
        var typeSet = true;
        try {
            if (applied === 'chapter' && U.has(marker, 'setTypeAsChapter')) {
                marker.setTypeAsChapter();
            } else if (applied === 'weblink' && U.has(marker, 'setTypeAsWebLink')) {
                marker.setTypeAsWebLink();
            } else if (applied === 'segmentation' &&
                       U.has(marker, 'setTypeAsSegmentation')) {
                marker.setTypeAsSegmentation();
            } else if (U.has(marker, 'setTypeAsComment')) {
                marker.setTypeAsComment();
            } else {
                typeSet = false;
            }
        } catch (e5) { typeSet = false; }

        var result = { added: describe(marker), scope: target.scope };
        if (!typeSet && applied !== 'comment') {
            result.note = 'Marker created, but the "' + applied + '" type is not '
                + 'settable on this build; it is a comment marker.';
        }
        return result;
    }

    function remove(params) {
        var target = collectionFor(params);
        var markers = each(target.markers);
        var removed = 0;

        for (var i = markers.length - 1; i >= 0; i--) {
            var entry = describe(markers[i]);
            var wanted = false;
            if (params.all) {
                wanted = true;
            } else if (params.at !== undefined) {
                wanted = Math.abs(entry.time - Number(params.at)) <= 0.05;
            } else if (params.range) {
                wanted = entry.time >= Number(params.range[0]) &&
                         entry.time <= Number(params.range[1]);
            } else if (params.name) {
                wanted = U.nameMatches(entry.name, params.name);
            }
            if (!wanted) { continue; }
            try { target.markers.deleteMarker(markers[i]); removed++; }
            catch (e) { /* skip stubborn markers rather than abort */ }
        }
        return { removed: removed };
    }

    // ------------------------------------------------------------------
    // Copy attributes
    // ------------------------------------------------------------------

    var INTRINSIC = ['Motion', 'Opacity', 'Time Remapping', 'Volume',
                     'Channel Volume', 'Panner'];

    function isIntrinsic(name) {
        for (var i = 0; i < INTRINSIC.length; i++) {
            if (U.nameMatches(name, INTRINSIC[i])) { return true; }
        }
        return false;
    }

    /**
     * Read a clip's effects and parameter values (with keyframes) so they can
     * be replayed onto another clip.
     */
    function readAttributes(clip, include) {
        var out = { effects: [], transform: [] };

        for (var i = 0; i < clip.components.numItems; i++) {
            var component = clip.components[i];
            var name = String(component.displayName);
            var intrinsic = isIntrinsic(name);

            if (intrinsic && !include.transform) { continue; }
            if (!intrinsic && !include.effects) { continue; }

            var entry = { name: name, params: [] };
            var count = 0;
            try { count = component.properties.numItems; } catch (e) { count = 0; }

            for (var p = 0; p < count; p++) {
                var param = component.properties[p];
                var record = { name: safe(function () {
                    return String(param.displayName);
                }, '') };
                if (!record.name) { continue; }

                var animated = NovaParams.safeCall(param, 'isTimeVarying', false);
                if (animated && include.keyframes) {
                    record.keyframes = readKeys(param, clip);
                    if (!record.keyframes.length) { continue; }
                } else {
                    var value = safe(function () { return param.getValue(); },
                                     undefined);
                    if (value === undefined) { continue; }
                    record.value = value;
                }
                entry.params[entry.params.length] = record;
            }

            if (intrinsic) { out.transform[out.transform.length] = entry; }
            else { out.effects[out.effects.length] = entry; }
        }
        return out;
    }

    function readKeys(param, clip) {
        var out = [];
        try {
            var keys = param.getKeys();
            for (var i = 0; i < keys.length; i++) {
                var record = {
                    time: NovaParams.fromParamTime(
                        clip, U.secondsOf(keys[i]), 'clip')
                };
                try { record.value = param.getValueAtKey(keys[i]); }
                catch (e) { continue; }
                try {
                    record.interp = NovaParams.interpName(
                        param.getInterpolationTypeAtKey(keys[i]));
                } catch (e2) { /* optional */ }
                out[out.length] = record;
            }
        } catch (e3) { /* not readable */ }
        return out;
    }

    function copyAttributes(params) {
        var source = T.resolveOne(params.from);
        var targets = T.resolve(params.to);
        var include = {
            effects: params.include ? contains(params.include, 'effects') : true,
            transform: params.include ? contains(params.include, 'transform') : true,
            keyframes: params.include ? contains(params.include, 'keyframes') : true
        };

        var attributes = readAttributes(source.clip, include);
        var applied = [];
        var problems = [];

        for (var t = 0; t < targets.length; t++) {
            var target = targets[t];
            if (target.clip === source.clip) { continue; }

            var summary = { clip: String(target.clip.name), effects: 0, params: 0 };

            if (params.replace && include.effects) {
                try {
                    NovaEffects.remove({ clip: { id: T.clipId(
                        target.clip, target.type, target.trackIndex,
                        target.clipIndex) }, all: true });
                } catch (e) { /* nothing removable; carry on */ }
            }

            for (var e2 = 0; e2 < attributes.effects.length; e2++) {
                var effect = attributes.effects[e2];
                try {
                    NovaEffects.apply({
                        clip: { id: T.clipId(target.clip, target.type,
                                             target.trackIndex, target.clipIndex) },
                        effect: effect.name, reuse: true
                    });
                    summary.effects++;
                } catch (e3) {
                    problems[problems.length] = {
                        clip: summary.clip, effect: effect.name,
                        reason: String(e3.message || e3)
                    };
                    continue;
                }
                summary.params += writeParams(target.clip, effect);
            }

            for (var m = 0; m < attributes.transform.length; m++) {
                summary.params += writeParams(target.clip, attributes.transform[m]);
            }
            applied[applied.length] = summary;
        }

        var result = {
            from: String(source.clip.name),
            copied_to: applied.length,
            clips: applied,
            included: include
        };
        if (problems.length) { result.problems = problems; }
        if (!applied.length) {
            result.note = 'No target clips were distinct from the source.';
        }
        return result;
    }

    function contains(list, value) {
        for (var i = 0; i < list.length; i++) {
            if (String(list[i]).toLowerCase() === value) { return true; }
        }
        return false;
    }

    function writeParams(clip, entry) {
        var written = 0;
        var component;
        try {
            component = NovaParams.findComponent(clip, entry.name, 0);
        } catch (e) { return 0; }

        for (var i = 0; i < entry.params.length; i++) {
            var record = entry.params[i];
            var param;
            try { param = NovaParams.findParam(component, record.name); }
            catch (e2) { continue; }

            if (record.keyframes) {
                try {
                    if (NovaParams.safeCall(param, 'isTimeVarying', false)) {
                        param.setTimeVarying(false);
                    }
                    param.setTimeVarying(true);
                    for (var k = 0; k < record.keyframes.length; k++) {
                        var key = record.keyframes[k];
                        var time = U.timeFromSeconds(
                            NovaParams.paramTime(clip, key.time, 'clip'));
                        param.addKey(time);
                        param.setValueAtKey(time, key.value, true);
                        if (key.interp) {
                            try {
                                param.setInterpolationTypeAtKey(
                                    time, NovaParams.INTERP[key.interp], true);
                            } catch (e3) { /* mode is a refinement */ }
                        }
                    }
                    written++;
                } catch (e4) { /* skip this parameter */ }
            } else {
                try { param.setValue(record.value, true); written++; }
                catch (e5) { /* read-only parameter */ }
            }
        }
        return written;
    }

    return {
        list: list,
        add: add,
        remove: remove,
        copyAttributes: copyAttributes,
        readAttributes: readAttributes
    };
}());
