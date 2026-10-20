// The Geometry node's viewer (geometry_view.py): the scene converted by Qt's balsam (scene_cache.py: OBJ, FBX, glTF /
// GLB, PLY, STL, DAE, 3DS..., Alembic through Blender) and loaded with Loader3D - meshes, materials, skins, cameras
// and animations (Timelines the app drives frame by frame: animMs). A second file may bring more cameras (and their
// animation): cameraSource, its meshes hidden.
// Placement: where the file puts it, or - "At origin" - standing at the origin by its bounding box (centred in X
// and Z, its lowest point at Y = 0).
// Cameras: "Default" = the viewer's own orbit camera; Mouse (Maya / Houdini): left drag orbits, middle drag pans,
// right drag dollies toward the point clicked; the wheel dollies toward the point under the cursor; double-click
// frames the model again. A file's camera (cameraName) is looked through as it is (animated too); the mouse then does
// nothing.
// Editing (the Edit Geometry node, `editable`): click an object to select it (Shift adds / removes); a handle like
// Houdini's - arrows move, rings rotate, end boxes scale along an axis, the centre box scales evenly; looking through
// a file's camera, the mouse moves THAT camera (orbit / pan / dolly as above). Every finished move goes to the app
// (commitSerial / commitJson), which keeps it as keys (auto-key on) or a set value (curves.py); the app's edits come
// back (`edits`) and are applied over the file's animation every frame - the same smooth curve as curves.py. On a
// channel the file animates, keys are OFFSETS added to the file's motion (an additive layer: the original stays).
// The handle takes the object's own orientation: it moves, turns and scales along the object's axes. Turning offsets
// are a rotation applied after the file's (about the object's own axes), not angles added to its Euler numbers.
// Orientation cube (top right): turns with the view; a click on a face looks straight at that side (a near-
// orthographic 1° lens far away - the overlays keep working); orbiting goes back to the perspective view.
// Views are never created / destroyed while the viewer lives (only shown / hidden): Qt's 3D renderer crashed in
// QSSGRenderRoot::markDirty when a view went away with its objects still being cleaned up. `alive` = false stops
// every timer before the app drops the viewer.
// Shading: the file's own materials and textures, or (textures off) a grey Blinn on every mesh; wireframe: the
// edges drawn over the shaded model (a second, see-through View3D with its OWN loader of the same scene - views
// sharing one scene crashed on teardown - whose camera is moved with this one); multisample antialiasing on / off.
// Light: key + fill + rim with the key casting a shadow, or one headlight from the camera. Ground: a square under the
// model, twice its larger footprint (the shadow falls on it). The scene shows every model ~100 units big (Qt's
// shadows need that scale).
import QtQuick
import QtQuick3D
import QtQuick3D.Helpers

Item {
    id: root
    property url source          // the converted scene (a QML file)
    property url cameraSource    // another converted file whose cameras are offered too ("" = none)
    property string cameraName: ""  // "" / "Default" = the orbit camera, else one of cameraNames
    // cameras the converter brought in from FBX: FBX (and Assimp, which balsam uses) has a camera look along its +X,
    // balsam leaves that out (Qt's cameras look along -Z) - so they are turned -90° about their up axis here; and their
    // field of view arrives halved (the file says 54.4°, the scene 27.2°). Both measured 2026-10-01 against Blender's
    // own view through the same camera; for the scene and the camera file separately
    property bool sceneFbx: false
    property bool camFileFbx: false
    property real animMs: 0      // the animation's time, in the scene's milliseconds
    property bool headlight: false
    property bool ground: false
    property bool atOrigin: false
    property bool textures: true
    property bool wire: false
    property bool msaa: true
    property bool grid: true
    // a camera to come back to (the app keeps each node's last view): [pivot x, y, z, yaw, pitch, dist]; used once,
    // after the model is measured, instead of framing it
    property var restore: []
    readonly property var cameraState: [pivot.x, pivot.y, pivot.z, yaw, pitch, dist, orthoView ? 1 : 0]
    property bool alive: true      // false: the app is dropping this viewer - nothing more is changed
    property bool orthoView: false // looking straight at a side (the orientation cube)
    readonly property bool keyRig: !headlight

    // the bounding box, measured from the loaded meshes: each mesh's box corners mapped into the loader's space
    property vector3d bmin: Qt.vector3d(0, 0, 0)
    property vector3d bmax: Qt.vector3d(0, 0, 0)
    property bool measured: false
    property var meshes: []     // the loaded Models
    property var ownMaterials: []  // each one's materials as the file made them (grey swaps them, never loses them)
    readonly property vector3d size: measured ? bmax.minus(bmin) : Qt.vector3d(0, 0, 0)
    readonly property real extent: Math.max(size.x, size.y, size.z, 0.001)
    // the placement: nothing, or the shift that stands the box at the origin
    readonly property vector3d offset: (measured && atOrigin)
        ? Qt.vector3d(-(bmin.x + bmax.x) / 2, -bmin.y, -(bmin.z + bmax.z) / 2) : Qt.vector3d(0, 0, 0)
    readonly property vector3d centre: measured ? bmin.plus(bmax).times(0.5).plus(offset) : Qt.vector3d(0, 0, 0)
    readonly property real floorY: measured ? bmin.y + offset.y : 0
    // the scene shows every model about 100 units big (k): Qt's shadows only work at that kind of scale (a 1-unit
    // model casts none, whatever the bias). Sizes and positions reported to the app stay in the file's units.
    readonly property real k: measured ? 100 / extent : 1
    readonly property vector3d sCentre: centre.times(k)
    readonly property real sFloor: floorY * k
    readonly property real sExtent: extent * k

    // ---- what the file brought: animation and cameras --------------------------------------------------------------
    property var timelines: []      // the scene's and the camera file's (driven together)
    property var wireTimelines: []  // the wireframe view's own copy of the scene
    property real durationMs: 0     // the longest timeline
    property var fileCams: []       // PerspectiveCameras from the scene, then from the camera file
    property var cameraNames: []    // their names, as offered (a repeated name gets " (2)")
    property var camFbx: []         // per camera: brought in from FBX (see sceneFbx)
    property real fileFps: 0        // the frame rate the file records (0: not recorded - glTF keeps seconds)
    property real recordedFps: 0    // the rate the conversion read from the file when the scene keeps none (BVH)
    readonly property int activeIndex: cameraNames.indexOf(cameraName)
    readonly property var activeCam: activeIndex >= 0 ? fileCams[activeIndex] : null
    readonly property bool activeFbx: activeIndex >= 0 && camFbx[activeIndex] === true
    function camRotation(c, fbx) {  // where it looks (FBX: its +X made the view direction - see sceneFbx)
        return fbx ? c.sceneRotation.times(Qt.quaternion(0.70710678, 0, -0.70710678, 0)) : c.sceneRotation
    }
    function camFov(c, fbx) {
        return fbx ? Math.min(c.fieldOfView * 2, 179) : c.fieldOfView
    }

    // read by the app: "" while loading / no file, "ok", or an error; vectors as plain numbers
    readonly property string loadState: loader.status === Loader3D.Ready ? (measured ? "ok" : "")
                                        : loader.status === Loader3D.Error ? "error: the converted scene didn't load" : ""
    readonly property real sizeX: size.x
    readonly property real sizeY: size.y
    readonly property real sizeZ: size.z
    readonly property real centreX: centre.x
    readonly property real centreY: centre.y
    readonly property real centreZ: centre.z

    onSourceChanged: { measured = false; meshes = []; ownMaterials = [] }
    onTexturesChanged: shade()
    onAnimMsChanged: { drive(timelines); drive(wireTimelines); applyEdits(); follow() }
    onActiveCamChanged: follow()
    onAtOriginChanged: Qt.callLater(frame)  // after the placement has followed

    function drive(list) {
        for (var i = 0; i < list.length; i++)
            list[i].currentFrame = animMs
    }
    function timelinesOf(item) {  // balsam's --manualAnimations list, switched on
        var list = item && item.timelineList !== undefined ? item.timelineList : []
        var out = []
        for (var i = 0; i < list.length; i++) {
            list[i].enabled = true
            out.push(list[i])
        }
        return out
    }
    function camerasOf(item) {
        var found = []
        function walk(node) {
            if (node.fieldOfView !== undefined && node.clipNear !== undefined)
                found.push(node)
            for (var c = 0; c < node.children.length; c++)
                walk(node.children[c])
        }
        if (item)
            walk(item)
        return found
    }
    function collect() {  // after either loader finished (a failure is said, not swallowed by the loader)
        try {
            collectNow()
        } catch (e) {
            console.warn("geometry viewer: collecting the scene failed: " + e + " " + (e.stack || ""))
        }
    }
    function collectNow() {  // the timelines and cameras of both loaders
        var tl = timelinesOf(loader.item).concat(timelinesOf(camLoader.item))
        var end = 0, fps = 0
        for (var i = 0; i < tl.length; i++) {
            end = Math.max(end, tl[i].endFrame)
            // balsam writes the file's rate when it records one, else 1000 (its milliseconds)
            if (tl[i].framesPerSecond !== undefined && tl[i].framesPerSecond > 0 && tl[i].framesPerSecond !== 1000)
                fps = tl[i].framesPerSecond
        }
        timelines = tl
        durationMs = end
        fileFps = fps || recordedFps
        drive(tl)
        var own = camerasOf(loader.item), lent = camerasOf(camLoader.item)
        var cams = own.concat(lent), names = [], fbx = []
        for (var f = 0; f < cams.length; f++)
            fbx.push(f < own.length ? sceneFbx : camFileFbx)
        for (var c = 0; c < cams.length; c++) {
            var name = cams[c].objectName || ("Camera " + (c + 1)), n = 2, candidate = name
            while (names.indexOf(candidate) >= 0 || candidate === "Default")
                candidate = name + " (" + n++ + ")"
            names.push(candidate)
        }
        fileCams = cams
        camFbx = fbx
        cameraNames = names
        if (camLoader.item) {  // a camera file only lends its cameras
            var extra = models(camLoader.item)
            for (var m = 0; m < extra.length; m++)
                extra[m].visible = false
        }
        buildObjects()
        applyEdits()
        sample()
        follow()
    }

    function shade() {  // the file's materials, or the grey Blinn on every mesh
        for (var m = 0; m < meshes.length; m++)
            meshes[m].materials = textures ? ownMaterials[m] : [blinn]
    }
    function models(top) {  // the Models under a node
        var found = []
        function walk(node) {
            if (node !== top && node.bounds !== undefined && node.materials !== undefined)
                found.push(node)
            for (var k = 0; k < node.children.length; k++)
                walk(node.children[k])
        }
        if (top)
            walk(top)
        return found
    }

    function measure() {
        if (!loader.item)
            return
        var lo = [1e30, 1e30, 1e30], hi = [-1e30, -1e30, -1e30]
        var found = models(loader.item)
        for (var f = 0; f < found.length; f++) {
            var b = found[f].bounds
            for (var i = 0; i < 8; i++) {
                var c = Qt.vector3d(i & 1 ? b.maximum.x : b.minimum.x, i & 2 ? b.maximum.y : b.minimum.y,
                                    i & 4 ? b.maximum.z : b.minimum.z)
                var p = loader.mapPositionFromScene(found[f].mapPositionToScene(c))
                lo = [Math.min(lo[0], p.x), Math.min(lo[1], p.y), Math.min(lo[2], p.z)]
                hi = [Math.max(hi[0], p.x), Math.max(hi[1], p.y), Math.max(hi[2], p.z)]
            }
        }
        if (found.length > 0 && (hi[0] - lo[0]) + (hi[1] - lo[1]) + (hi[2] - lo[2]) > 0) {
            var own = []
            for (var m = 0; m < found.length; m++) {
                found[m].pickable = true  // the dolly aims at the point clicked on it
                var list = []
                for (var j = 0; j < found[m].materials.length; j++)
                    list.push(found[m].materials[j])
                own.push(list)
            }
            ownMaterials = own
            meshes = found
            shade()
            bmin = Qt.vector3d(lo[0], lo[1], lo[2])
            bmax = Qt.vector3d(hi[0], hi[1], hi[2])
            measured = true
            Qt.callLater(restore.length >= 6 ? comeBack : frame)
        }
    }
    function shadowFacts() {  // for tests: do the loaded meshes cast / receive shadows?
        var out = []
        for (var m = 0; m < meshes.length; m++)
            out.push("casts=" + meshes[m].castsShadows + " receives=" + meshes[m].receivesShadows)
        return out.join("; ")
    }
    DefaultMaterial {
        id: blinn
        diffuseColor: "#b0b0b0"
        specularAmount: 0.35
        specularRoughness: 0.35
        lighting: DefaultMaterial.FragmentLighting
    }

    Timer {  // the meshes get their boxes once loaded: measure until they have them
        interval: 100
        repeat: true
        running: root.alive && loader.status === Loader3D.Ready && !root.measured
        onTriggered: root.measure()
    }

    // ---- editing (the Edit Geometry node) -------------------------------------------------------------------------
    property bool editable: false
    property var edits: ({})          // {path: {keys / offsets: {channel: [[frame, value]]}, set: {channel: value}}}
    property real keyFps: 24          // the scene's frames (keys are in them)
    readonly property real frameNow: animMs / 1000 * keyFps + 1
    property var objects: ({})        // path → node (scene, camera file)
    property var wireObjects: ({})    // the wireframe view's copy
    property var rest: ({})           // path → channels as loaded (an object nothing animates goes back to these)
    property var animated: ({})       // path → [channels the file animates]
    property string animatedJson: "{}"
    property var camPaths: []         // fileCams' paths
    readonly property string activeCamPath: activeIndex >= 0 && activeIndex < camPaths.length ? camPaths[activeIndex] : ""
    property var selection: []        // selected object paths
    property int commitSerial: 0      // the app reads commitJson when this changes
    property string commitJson: ""
    property int keySerial: 0         // the app asks for a key on everything selected (Set Key)
    property string samplesJson: "{}" // the file's own curves for the curve editor: {path: {channel: [[frame, v]]}}
    readonly property var channelNames: ["tx", "ty", "tz", "rx", "ry", "rz", "sx", "sy", "sz"]

    onEditsChanged: reapply()
    onKeySerialChanged: if (keySerial > 0) keyEverything()
    onSelectionChanged: { sample(); updateGizmo() }
    onActiveCamPathChanged: sample()

    function isNode(n) { return n && n.position !== undefined && n.eulerRotation !== undefined && n.scale !== undefined }
    function pathsOf(top, prefix, out) {
        if (!top)
            return
        var kids = top.children
        for (var i = 0; i < kids.length; i++) {
            var n = kids[i]
            if (!isNode(n))
                continue
            var p = prefix + "/" + (n.objectName || ("#" + i))
            out[p] = n
            pathsOf(n, p, out)
        }
    }
    function pathOf(node, map) {
        for (var p in map)
            if (map[p] === node)
                return p
        return ""
    }
    function channels(n) {
        return {tx: n.position.x, ty: n.position.y, tz: n.position.z, rx: n.eulerRotation.x, ry: n.eulerRotation.y,
                rz: n.eulerRotation.z, sx: n.scale.x, sy: n.scale.y, sz: n.scale.z}
    }
    function setChannels(n, v) {
        n.position = Qt.vector3d(v.tx, v.ty, v.tz)
        n.eulerRotation = Qt.vector3d(v.rx, v.ry, v.rz)
        n.scale = Qt.vector3d(v.sx, v.sy, v.sz)
    }
    function buildObjects() {  // after loading: every node by path, what the file animates, how each was loaded
        var map = {}
        pathsOf(loader.item, "scene", map)
        pathsOf(camLoader.item, "cameras", map)
        objects = map
        var r = {}
        for (var p in map)
            r[p] = channels(map[p])
        rest = r
        var anim = {}
        var props = {position: ["tx", "ty", "tz"], rotation: ["rx", "ry", "rz"], eulerRotation: ["rx", "ry", "rz"],
                     scale: ["sx", "sy", "sz"]}
        for (var i = 0; i < timelines.length; i++) {
            var groups = timelines[i].keyframeGroups
            for (var g = 0; g < groups.length; g++) {
                var tp = pathOf(groups[g].target, map), chs = props[groups[g].property]
                if (!tp || !chs)
                    continue
                anim[tp] = (anim[tp] || []).concat(chs.filter(function(c) { return (anim[tp] || []).indexOf(c) < 0 }))
            }
        }
        animated = anim
        animatedJson = JSON.stringify(anim)
        var cp = []
        for (var c = 0; c < fileCams.length; c++)
            cp.push(pathOf(fileCams[c], map))
        camPaths = cp
    }
    // the same curve as curves.py: monotone cubic (Fritsch-Carlson), flat outside the keys
    function evalCurve(keys, f) {
        var n = keys.length
        if (n === 0) return undefined
        if (n === 1 || f <= keys[0][0]) return keys[0][1]
        if (f >= keys[n - 1][0]) return keys[n - 1][1]
        var d = [], m = []
        for (var i = 0; i < n - 1; i++)
            d.push((keys[i + 1][1] - keys[i][1]) / Math.max(1e-9, keys[i + 1][0] - keys[i][0]))
        m.push(d[0])
        for (i = 1; i < n - 1; i++)
            m.push(d[i - 1] * d[i] <= 0 ? 0 : (d[i - 1] + d[i]) / 2)
        m.push(d[n - 2])
        for (i = 0; i < n - 1; i++) {
            if (d[i] === 0) { m[i] = 0; m[i + 1] = 0; continue }
            var a = m[i] / d[i], b = m[i + 1] / d[i], q = a * a + b * b
            if (q > 9) { var t = 3 / Math.sqrt(q); m[i] = t * a * d[i]; m[i + 1] = t * b * d[i] }
        }
        for (i = 0; i < n - 1; i++)
            if (keys[i][0] <= f && f <= keys[i + 1][0])
                break
        var h = keys[i + 1][0] - keys[i][0], u = (f - keys[i][0]) / h, u2 = u * u, u3 = u2 * u
        return (2 * u3 - 3 * u2 + 1) * keys[i][1] + (u3 - 2 * u2 + u) * h * m[i] + (-2 * u3 + 3 * u2) * keys[i + 1][1]
               + (u3 - u2) * h * m[i + 1]
    }
    function offsetNow(p, ch) {  // the additive layer's offset on a channel at this frame (0: none)
        var e = edits[p], ks = e && e.offsets ? e.offsets[ch] : undefined
        return (ks && ks.length) ? evalCurve(ks, frameNow) : 0
    }
    function curveOr(list, dflt) { return (list && list.length) ? evalCurve(list, frameNow) : dflt }
    function rotOffset(e) {  // the turning offset (Euler degrees) at this frame, or null
        var o = e.offsets || {}
        if (!((o.rx && o.rx.length) || (o.ry && o.ry.length) || (o.rz && o.rz.length)))
            return null
        return Qt.vector3d(curveOr(o.rx, 0), curveOr(o.ry, 0), curveOr(o.rz, 0))
    }
    function applyTo(map) {
        for (var p in edits) {
            var n = map[p]
            if (!n)
                continue
            var e = edits[p], v = channels(n), any = false
            var keys = e.keys || {}, offs = e.offsets || {}, set = e.set || {}
            var absRot = (keys.rx && keys.rx.length) || (keys.ry && keys.ry.length) || (keys.rz && keys.rz.length)
            var turn = absRot ? null : rotOffset(e)
            var fileRot = quat(n.rotation)  // the file's (just set) - an offset turns it further
            for (var c = 0; c < channelNames.length; c++) {
                var ch = channelNames[c]
                if (turn && ch.charAt(0) === "r")
                    continue
                var val = (keys[ch] && keys[ch].length) ? evalCurve(keys[ch], frameNow)
                        : (offs[ch] && offs[ch].length) ? v[ch] + evalCurve(offs[ch], frameNow)
                        : (set[ch] !== undefined ? set[ch] : undefined)
                if (val !== undefined) { v[ch] = val; any = true }
            }
            if (any)
                setChannels(n, v)
            if (turn)
                n.rotation = fileRot.times(Quaternion.fromEulerAngles(turn.x, turn.y, turn.z))
        }
    }
    function eulerOf(q) {  // a rotation as Qt's Euler angles (through a node that converts)
        eulerHelper.rotation = q
        return v3(eulerHelper.eulerRotation)
    }
    function wantedOffsets(p, start, startRot, n) {
        // after a move: the offsets that give what is shown now - translate / scale: now - the file's value (= the
        // start minus the offset there); turning: the rotation from the file's to now, as Euler angles
        var out = {}, chs = animated[p] || [], now = channels(n), e = edits[p] || {}
        var keys = e.keys || {}
        var turned = false
        for (var c = 0; c < chs.length; c++) {
            var ch = chs[c]
            if (keys[ch] && keys[ch].length)
                continue
            if (ch.charAt(0) === "r") { turned = true; continue }
            out[ch] = now[ch] - (start[ch] - offsetNow(p, ch))
        }
        if (turned) {
            var o = Qt.vector3d(offsetNow(p, "rx"), offsetNow(p, "ry"), offsetNow(p, "rz"))
            var fileRot = startRot.times(Quaternion.fromEulerAngles(o.x, o.y, o.z).inverted())
            var d = eulerOf(fileRot.inverted().times(quat(n.rotation)))
            out.rx = d.x; out.ry = d.y; out.rz = d.z
        }
        return out
    }
    function applyEdits() {  // always from the file's own state (rest / its animation at this time), then the edits -
        for (var p in objects)  // so an additive offset is never added twice
            if (!animated[p] && rest[p])
                setChannels(objects[p], rest[p])
        for (p in wireObjects)
            if (!animated[p] && rest[p])
                setChannels(wireObjects[p], rest[p])
        nudgeTimelines(timelines)
        nudgeTimelines(wireTimelines)
        applyTo(objects)
        applyTo(wireObjects)
        updateGizmo()
    }
    function reapply() {  // the edits changed
        applyEdits()
        follow()
    }
    function nudgeTimelines(list) {  // the same time set again only after a change: the file's values come back
        for (var i = 0; i < list.length; i++) {
            list[i].currentFrame = animMs + 0.001
            list[i].currentFrame = animMs
        }
    }
    function sample() {  // the file's own animation of what the curve editor shows (sampled, max ~400 points)
        if (!editable)
            return
        var paths = selection.length ? selection : (activeCamPath ? [activeCamPath] : [])
        paths = paths.filter(function(p) { return animated[p] && objects[p] })
        var out = {}
        if (paths.length && durationMs > 0) {
            var frames = Math.floor(durationMs / 1000 * keyFps) + 1, step = Math.max(1, Math.ceil(frames / 400))
            for (var f = 1; f <= frames + step - 1; f += step) {
                var fr = Math.min(f, frames)
                for (var i = 0; i < timelines.length; i++)
                    timelines[i].currentFrame = (fr - 1) * 1000 / keyFps
                for (var k = 0; k < paths.length; k++) {
                    var v = channels(objects[paths[k]]), o = out[paths[k]] = out[paths[k]] || {}
                    var chs = animated[paths[k]]
                    for (var c = 0; c < chs.length; c++)
                        (o[chs[c]] = o[chs[c]] || []).push([fr, v[chs[c]]])
                }
            }
            for (var pth in out)  // turning: no jumps where the angle wraps past ±180
                for (var rc in out[pth])
                    if (rc.charAt(0) === "r") {
                        var pts = out[pth][rc]
                        for (var j = 1; j < pts.length; j++) {
                            while (pts[j][1] - pts[j - 1][1] > 180) pts[j][1] -= 360
                            while (pts[j][1] - pts[j - 1][1] < -180) pts[j][1] += 360
                        }
                    }
            nudgeTimelines(timelines)
            applyEdits()
        }
        samplesJson = JSON.stringify(out)
    }
    function send(moves, keyAll, offsets) {  // a finished move → the app (it decides: keys, offsets or set values)
        commitJson = JSON.stringify({frame: Math.round(frameNow), moves: moves, keyAll: keyAll === true,
                                     offsets: offsets || {}})
        commitSerial++
    }
    function keyEverything() {  // Set Key: what is selected, else the camera looked through (offsets as they are now)
        var paths = selection.length ? selection : (activeCamPath ? [activeCamPath] : [])
        var moves = {}, offs = {}
        for (var i = 0; i < paths.length; i++) {
            var n = objects[paths[i]]
            if (!n)
                continue
            moves[paths[i]] = channels(n)
            offs[paths[i]] = wantedOffsets(paths[i], channels(n), quat(n.rotation), n)
        }
        if (paths.length)
            send(moves, true, offs)
    }

    // selecting: the object under the mouse (Shift: add / remove)
    function pickSelect(x, y, add) {
        var hit = view.pick(x, y)
        var p = hit.objectHit ? pathOf(hit.objectHit, objects) : ""
        if (!p) {
            if (!add)
                selection = []
            return
        }
        var sel = add ? selection.slice() : []
        var at = sel.indexOf(p)
        if (at >= 0) sel.splice(at, 1); else sel.push(p)
        selection = sel
    }

    // the handle: where the first selected object is, a constant size on screen
    property vector3d gizmoPos: Qt.vector3d(0, 0, 0)
    property quaternion gizmoRot: Qt.quaternion(1, 0, 0, 0)  // the object's own orientation
    property real gizmoScale: 1
    property var gizmoCam: null
    property var gizmoView: null
    function updateGizmo() {
        var n = selection.length ? objects[selection[0]] : null
        if (!n)
            return
        gizmoPos = v3(n.scenePosition)
        gizmoRot = quat(n.sceneRotation)
        var cam = view.camera
        gizmoScale = Math.max(1e-4, cam.scenePosition.minus(gizmoPos).length() * 0.0012)
    }
    // dragging a handle
    property string dragHandle: ""
    property var dragStart: null
    // Qt 6 hands out a vector / quaternion read from a node as a LIVE reference (read again on every use): a drag's
    // starting state must be copied, or it moves along with the object and every mouse move adds up (seen: 40 px of
    // mouse moved the object 300 px, a camera turned 140° for 40°)
    function v3(v) { return Qt.vector3d(v.x, v.y, v.z) }
    function quat(q) { return Qt.quaternion(q.scalar, q.x, q.y, q.z) }
    function axisOf(h) {
        var a = h.charAt(2)
        return a === "x" ? Qt.vector3d(1, 0, 0) : a === "y" ? Qt.vector3d(0, 1, 0) : Qt.vector3d(0, 0, 1)
    }
    function startHandle(h, x, y) {
        var nodes = []
        for (var i = 0; i < selection.length; i++) {
            var n = objects[selection[i]]
            if (n)
                nodes.push({path: selection[i], node: n, pos: v3(n.scenePosition), rot: quat(n.sceneRotation),
                            scale: v3(n.scale), parentRot: n.parent && n.parent.sceneRotation
                                                       ? quat(n.parent.sceneRotation) : Qt.quaternion(1, 0, 0, 0),
                            start: channels(n), startRot: quat(n.rotation)})
        }
        var c0 = v3(gizmoPos), L = gizmoScale * 100
        var s0 = v3(view.mapFrom3DScene(c0))
        var info = {x: x, y: y, nodes: nodes, centre: s0}
        if (h !== "s:all") {  // the object's own axis, in the scene
            var A = quat(gizmoRot).times(axisOf(h)).normalized(), s1 = view.mapFrom3DScene(c0.plus(A.times(L)))
            var dx = s1.x - s0.x, dy = s1.y - s0.y, len = Math.max(1e-6, Math.sqrt(dx * dx + dy * dy))
            info.axis = A
            info.u = Qt.vector2d(dx / len, dy / len)
            info.pxPerUnit = len / L
            info.L = L
            // turning follows the mouse round the handle: seen from the side the axis points to, a positive turn
            // runs counter-clockwise - the screen's angle (y down) runs clockwise
            info.facing = v3(view.camera.forward).dotProduct(A) < 0 ? -1 : 1
        }
        dragHandle = h
        dragStart = info
    }
    function moveHandle(x, y) {
        var st = dragStart, kind = dragHandle.charAt(0)
        for (var i = 0; i < st.nodes.length; i++) {
            var e = st.nodes[i], n = e.node
            if (kind === "t") {
                var m = ((x - st.x) * st.u.x + (y - st.y) * st.u.y) / st.pxPerUnit
                n.position = n.parent.mapPositionFromScene(e.pos.plus(st.axis.times(m)))
            } else if (kind === "r") {
                var a0 = Math.atan2(st.y - st.centre.y, st.x - st.centre.x), a1 = Math.atan2(y - st.centre.y, x - st.centre.x)
                var deg = (a1 - a0) * 180 / Math.PI * st.facing
                var q = Quaternion.fromAxisAndAngle(st.axis, deg).times(e.rot)
                n.rotation = e.parentRot.inverted().times(q)
            } else if (dragHandle === "s:all") {
                var k = Math.max(0.01, 1 + (x - st.x) / 150)
                n.scale = e.scale.times(k)
            } else {
                var mm = ((x - st.x) * st.u.x + (y - st.y) * st.u.y) / st.pxPerUnit
                var f = Math.max(0.01, 1 + mm / st.L)
                var a = dragHandle.charAt(2)
                n.scale = Qt.vector3d(e.scale.x * (a === "x" ? f : 1), e.scale.y * (a === "y" ? f : 1),
                                      e.scale.z * (a === "z" ? f : 1))
            }
        }
        updateGizmo()
    }
    function endHandle() {
        var moves = {}, offs = {}, kind = dragHandle.charAt(0)
        var only = kind === "t" ? ["tx", "ty", "tz"] : kind === "r" ? ["rx", "ry", "rz"] : ["sx", "sy", "sz"]
        for (var i = 0; i < dragStart.nodes.length; i++) {
            var e = dragStart.nodes[i], v = channels(e.node), part = {}, want = {}
            var all = wantedOffsets(e.path, e.start, e.startRot, e.node)
            for (var c = 0; c < only.length; c++) {
                part[only[c]] = v[only[c]]
                if (all[only[c]] !== undefined)
                    want[only[c]] = all[only[c]]
            }
            moves[e.path] = part
            offs[e.path] = want
        }
        dragHandle = ""
        dragStart = null
        send(moves, false, offs)
    }

    // moving the camera looked through (editing): orbit about a point in front of it, pan, dolly
    property var camStart: null
    readonly property var fbxFix: Qt.quaternion(0.70710678, 0, -0.70710678, 0)
    function camBegin() {
        var pos = v3(lookCam.position), rot = quat(lookCam.rotation)
        var d = Math.max(sExtent * 0.05, pos.minus(sCentre).length())
        camStart = {pos: pos, rot: rot, d: d, pivot: pos.plus(rot.times(Qt.vector3d(0, 0, -1)).times(d)),
                    start: activeCam ? channels(activeCam) : {}, startRot: activeCam ? quat(activeCam.rotation) : null}
    }
    function camWrite(pos, rot) {  // a view position / rotation → the file camera's own (parent's) transform
        var c = activeCam
        var fileRot = activeFbx ? rot.times(fbxFix.inverted()) : rot
        var parentRot = c.parent && c.parent.sceneRotation ? quat(c.parent.sceneRotation) : Qt.quaternion(1, 0, 0, 0)
        c.rotation = parentRot.inverted().times(fileRot)
        c.position = c.parent.mapPositionFromScene(pos)
        follow()
    }
    function camOrbit(dx, dy) {
        var st = camStart, right = st.rot.times(Qt.vector3d(1, 0, 0))
        var q = Quaternion.fromAxisAndAngle(Qt.vector3d(0, 1, 0), -dx * 0.4).times(
                    Quaternion.fromAxisAndAngle(right, -dy * 0.4))
        camWrite(st.pivot.plus(q.times(st.pos.minus(st.pivot))), q.times(st.rot))
    }
    function camPan(dx, dy) {
        var st = camStart, k = 2 * st.d * Math.tan(lookCam.fieldOfView * Math.PI / 360) / view.width
        var right = st.rot.times(Qt.vector3d(1, 0, 0)), up = st.rot.times(Qt.vector3d(0, 1, 0))
        camWrite(st.pos.plus(right.times(-dx * k)).plus(up.times(dy * k)), st.rot)
    }
    function camDolly(amount) {
        var st = camStart, fwd = st.rot.times(Qt.vector3d(0, 0, -1))
        camWrite(st.pos.plus(fwd.times(st.d * (1 - Math.exp(-amount)))), st.rot)
    }
    function camEnd() {
        var st = camStart
        camStart = null
        if (!activeCamPath)
            return
        var v = channels(activeCam), moves = {}, offs = {}
        moves[activeCamPath] = {tx: v.tx, ty: v.ty, tz: v.tz, rx: v.rx, ry: v.ry, rz: v.rz}
        offs[activeCamPath] = wantedOffsets(activeCamPath, st.start, st.startRot, activeCam)
        send(moves, false, offs)
    }
    Timer {  // the wheel's dolly on a file camera is sent once the wheel rests
        id: wheelCommit
        interval: 350
        onTriggered: if (root.alive) root.camEnd()
    }

    // ---- the orbit camera: a pivot (yaw, pitch, distance) ---------------------------------------------------------
    property vector3d pivot: Qt.vector3d(0, 0, 0)
    property real yaw: 32
    property real pitch: -18
    property real dist: 300

    function frame() {  // the whole model from the usual angle
        pivot = sCentre
        dist = sExtent * 2.2
        yaw = 32
        pitch = -18
        place()
    }
    function comeBack() {  // the view this node had before (the app hands it over)
        pivot = Qt.vector3d(restore[0], restore[1], restore[2])
        yaw = restore[3]
        pitch = restore[4]
        dist = restore[5]
        orthoView = restore.length > 6 && restore[6] === 1
        restore = []
        place()
    }
    property var wireCam: null  // the wireframe view's camera
    property var cubeCam: null
    // the orientation cube: look straight at a side - yaw / pitch of each, a 1° lens from far away (near-orthographic)
    function lookFrom(side) {
        var a = {FRONT: [0, 0], BACK: [180, 0], RIGHT: [90, 0], LEFT: [-90, 0], TOP: [0, -89.9], BOTTOM: [0, 89.9]}[side]
        if (!a)
            return
        orthoView = true
        pivot = sCentre
        yaw = a[0]
        pitch = a[1]
        dist = sExtent * 1.25 / Math.tan(0.5 * Math.PI / 180)  // the model about fills the view (1° lens), some room
        place()
    }
    function cubeFaceAt(x, y) {  // the cube's face under a point of the viewer ("" = none)
        if (!cubeView.visible || x < cubeView.x || y > cubeView.y + cubeView.height)
            return ""
        var hit = cubeView.pick(x - cubeView.x, y - cubeView.y)
        return hit.objectHit ? hit.objectHit.objectName : ""
    }
    function leaveOrtho() {  // back to the perspective view, from the same side
        if (!orthoView)
            return
        orthoView = false
        dist = sExtent * 2.2
        place()
    }
    function place() {
        var y = yaw * Math.PI / 180, p = pitch * Math.PI / 180
        var dir = Qt.vector3d(Math.sin(y) * Math.cos(p), -Math.sin(p), Math.cos(y) * Math.cos(p))
        camera.position = pivot.plus(dir.times(dist))
        camera.lookAt(pivot)
        follow()
    }
    function follow() {  // the wireframe, handle and cube views' cameras take the one looked through
        if (cubeCam && alive) {  // the cube is seen from where the view looks
            var cr = quat(view.camera.sceneRotation)
            cubeCam.rotation = cr
            cubeCam.position = cr.times(Qt.vector3d(0, 0, 330))
        }
        if (gizmoCam) {
            gizmoCam.position = view.camera.scenePosition
            gizmoCam.rotation = view.camera.sceneRotation
            gizmoCam.fieldOfView = view.camera.fieldOfView
            gizmoCam.fieldOfViewOrientation = view.camera.fieldOfViewOrientation
            gizmoCam.clipNear = view.camera.clipNear
            gizmoCam.clipFar = view.camera.clipFar
            updateGizmo()
            catchUp.restart()
        }
        if (!wireCam)
            return
        var c = activeCam
        if (c) {
            wireCam.position = c.scenePosition
            wireCam.rotation = camRotation(c, activeFbx)
            wireCam.fieldOfView = camFov(c, activeFbx)
            wireCam.fieldOfViewOrientation = c.fieldOfViewOrientation
            wireCam.clipNear = c.clipNear * k
            wireCam.clipFar = c.clipFar * k
        } else {
            wireCam.position = camera.position
            wireCam.rotation = camera.rotation
            wireCam.fieldOfView = camera.fieldOfView
            wireCam.fieldOfViewOrientation = camera.fieldOfViewOrientation
            wireCam.clipNear = camera.clipNear
            wireCam.clipFar = camera.clipFar
        }
        catchUp.restart()
    }
    // with the wireframe view on top, the main view draws one camera move behind (seen in tests: a single zoom stayed
    // stale until the next move) - a moment later an invisible change in its scene makes it draw again
    Timer {
        id: catchUp
        interval: 30
        onTriggered: if (root.alive) nudge.x = nudge.x === 0 ? 0.001 : 0
    }
    function aimAt(x, y) {  // the point under the mouse: on the model, else at the pivot's depth
        var hit = view.pick(x, y)
        if (hit.objectHit)
            return hit.scenePosition
        return camera.mapFromViewport(Qt.vector3d(x / view.width, y / view.height, dist))
    }
    function dolly(amount, target) {  // toward (amount > 0) or away from the target, the view's angle kept
        var s = Math.exp(-amount)
        pivot = target.plus(pivot.minus(target).times(s))
        dist = Math.max(dist * s, sExtent * 0.002)
        place()
    }

    View3D {
        id: view
        objectName: "mainView"
        anchors.fill: parent
        camera: root.activeCam ? lookCam : camera
        environment: SceneEnvironment {
            backgroundMode: SceneEnvironment.Color
            clearColor: "#262626"
            antialiasingMode: root.msaa ? SceneEnvironment.MSAA : SceneEnvironment.NoAA
            antialiasingQuality: SceneEnvironment.High
        }

        PerspectiveCamera {
            id: camera
            objectName: "camera"
            fieldOfView: root.orthoView ? 1 : 60
            // near / far follow the distance: zooming far out (or in) never cuts the scene away
            clipNear: root.orthoView ? Math.max(root.dist * 0.01, root.dist - root.sExtent * 20)
                                     : Math.max(root.sExtent * 0.0005, root.dist * 0.001)
            clipFar: root.dist + root.sExtent * 50
            DirectionalLight {  // headlight: from the camera
                visible: root.headlight && !root.activeCam
                brightness: 1.0
                ambientColor: Qt.rgba(0.12, 0.12, 0.12, 1)
                // no shadow: from the camera it hides behind the model (and only speckles it)
                shadowBias: 2
                shadowFactor: 55
                shadowMapQuality: Light.ShadowMapQualityHigh
                shadowMapFar: root.sExtent * 20
            }
        }
        PerspectiveCamera {  // looking through a file's camera: where it is in the scene (animated too), as it sees
            id: lookCam
            objectName: "lookCam"
            position: root.activeCam ? root.activeCam.scenePosition : Qt.vector3d(0, 0, 0)
            rotation: root.activeCam ? root.camRotation(root.activeCam, root.activeFbx) : Qt.quaternion(1, 0, 0, 0)
            fieldOfView: root.activeCam ? root.camFov(root.activeCam, root.activeFbx) : 60
            fieldOfViewOrientation: root.activeCam ? root.activeCam.fieldOfViewOrientation
                                                   : PerspectiveCamera.Vertical
            clipNear: root.activeCam ? root.activeCam.clipNear * root.k : 1
            clipFar: root.activeCam ? root.activeCam.clipFar * root.k : 10000
            DirectionalLight {  // the headlight goes with the camera looked through
                visible: root.headlight && root.activeCam !== null
                brightness: 1.0
                ambientColor: Qt.rgba(0.12, 0.12, 0.12, 1)
            }
        }

        DirectionalLight {  // key
            objectName: "keyLight"
            visible: root.keyRig
            // high (60°) and 40° beside the first view (yaw 32): the shadow falls short, behind and to the side,
            // on the ground - not hidden behind the model or past the ground's edge
            eulerRotation: Qt.vector3d(-60, 72, 0)
            brightness: 1.0
            ambientColor: Qt.rgba(0.1, 0.1, 0.1, 1)
            castsShadow: root.keyRig  // the 3-light rig always casts
            shadowBias: 2
            shadowFactor: 55
            shadowMapQuality: Light.ShadowMapQualityHigh
            shadowMapFar: root.sExtent * 20
        }
        DirectionalLight {  // fill: low, from the view's other side
            visible: root.keyRig
            eulerRotation: Qt.vector3d(-15, -28, 0)
            brightness: 0.4
        }
        DirectionalLight {  // rim: from behind
            visible: root.keyRig
            eulerRotation: Qt.vector3d(-30, 212, 0)
            brightness: 0.55
        }

        Node { id: nudge }  // moved by catchUp only (nothing in it)
        Node { id: eulerHelper }  // converts rotations to Euler angles (eulerOf)

        Node {  // the model (and the camera file): where the file puts it, or standing at the origin, k times its size
            position: root.offset.times(root.k)
            scale: Qt.vector3d(root.k, root.k, root.k)
            Loader3D {
                id: loader
                source: root.source
                onLoaded: root.collect()
            }
            Loader3D {
                id: camLoader
                source: root.cameraSource
                onLoaded: root.collect()
                onSourceChanged: if (source.toString() === "") Qt.callLater(root.collect)
            }
        }

        Model {  // the ground under the model: a square twice its larger footprint (#Rectangle is 100 units across)
            objectName: "ground"
            visible: root.ground && root.measured
            source: "#Rectangle"
            eulerRotation.x: -90
            position: Qt.vector3d(root.sCentre.x, root.sFloor - root.sExtent * 0.001, root.sCentre.z)
            scale: Qt.vector3d(Math.max(root.size.x, root.size.z) * 2 * root.k / 100,
                               Math.max(root.size.x, root.size.z) * 2 * root.k / 100, 1)
            castsShadows: false
            receivesShadows: true
            pickable: false
            materials: DefaultMaterial { diffuseColor: "#4a4a4a" }
        }

        AxisHelper {  // the ground grid at the origin (cells about a fifth of the model's size)
            visible: root.grid
            enableAxisLines: false
            enableXZGrid: true
            enableXYGrid: false
            enableYZGrid: false
            gridColor: "#5a5a5a"
            gridOpacity: 0.6
            scale: Qt.vector3d(root.sExtent / 500, root.sExtent / 500, root.sExtent / 500)
        }
    }

    // ---- wireframe over the model: a see-through view of its own (only while wire is on) ------------------------
    View3D {  // shown while wire is on; its scene loaded only then
        id: wireView
        anchors.fill: parent
        visible: root.wire && root.measured
        camera: wireCamera
        renderMode: View3D.Inline  // drawn into the same scene (a texture of its own stopped the view below updating)
        environment: SceneEnvironment {
            backgroundMode: SceneEnvironment.Transparent
            antialiasingMode: root.msaa ? SceneEnvironment.MSAA : SceneEnvironment.NoAA
            antialiasingQuality: SceneEnvironment.High
            debugSettings: DebugSettings { wireframeEnabled: true }
        }
        PerspectiveCamera {  // moved with the camera looked through by follow() - not bound to it (a binding
            id: wireCamera   // between the two views' cameras left the main view without redraws)
            objectName: "wireCamera"
            Component.onCompleted: { root.wireCam = wireCamera; root.follow() }
        }
        Node {
            position: root.offset.times(root.k)
            scale: Qt.vector3d(root.k, root.k, root.k)
            Loader3D {
                id: wireLoader
                source: wireView.visible && root.alive ? root.source : ""
                onSourceChanged: { wireReady.done = false; root.wireTimelines = []; root.wireObjects = {} }
            }
        }
        Timer {  // its meshes: pale see-through unlit lines, back faces culled (only the edges facing the camera)
                id: wireReady
                interval: 100
                repeat: true
                running: root.alive && wireLoader.status === Loader3D.Ready && !done
                property bool done: false
                onTriggered: {
                    var found = root.models(wireLoader.item)
                    for (var m = 0; m < found.length; m++) {
                        found[m].materials = [wireMaterial]
                        found[m].pickable = false
                        found[m].castsShadows = false
                    }
                    if (found.length > 0) {
                        root.wireTimelines = root.timelinesOf(wireLoader.item)
                        root.drive(root.wireTimelines)
                        var map = {}
                        root.pathsOf(wireLoader.item, "scene", map)
                        root.wireObjects = map
                        root.applyEdits()
                        root.follow()
                        done = true
                    }
                }
        }
        DefaultMaterial {
            id: wireMaterial
            lighting: DefaultMaterial.NoLighting
            diffuseColor: "#b4c3d6"
            opacity: 0.45  // a dense mesh would otherwise turn solid
            cullMode: Material.BackFaceCulling
        }
    }

    // ---- the handle (editing): a see-through view of its own on top, so it is never hidden by the model ----------
    View3D {  // shown while something is selected (editing)
            id: gview
            anchors.fill: parent
            visible: root.editable && root.selection.length > 0 && root.measured
            camera: gcam
            renderMode: View3D.Inline
            environment: SceneEnvironment {
                backgroundMode: SceneEnvironment.Transparent
                antialiasingMode: root.msaa ? SceneEnvironment.MSAA : SceneEnvironment.NoAA
            }
            PerspectiveCamera {
                id: gcam
                Component.onCompleted: { root.gizmoCam = gcam; root.gizmoView = gview; root.follow() }
            }
            Node {
                position: root.gizmoPos
                rotation: root.gizmoRot  // turned with the object
                scale: Qt.vector3d(root.gizmoScale, root.gizmoScale, root.gizmoScale)
                Repeater3D {  // one arrow, ring and scale box per axis (built along Y, turned onto X / Z)
                    model: [{a: "x", c: "#e04848", r: Qt.vector3d(0, 0, -90)}, {a: "y", c: "#58c858", r: Qt.vector3d(0, 0, 0)},
                            {a: "z", c: "#4a78e8", r: Qt.vector3d(90, 0, 0)}]
                    delegate: Node {
                        required property var modelData
                        eulerRotation: modelData.r
                        Model {  // arrow shaft
                            objectName: "t:" + modelData.a
                            position: Qt.vector3d(0, 40, 0)
                            geometry: CylinderGeometry { radius: 2.2; length: 80 }
                            pickable: true
                            materials: DefaultMaterial { lighting: DefaultMaterial.NoLighting; diffuseColor: modelData.c }
                        }
                        Model {  // arrow head
                            objectName: "t:" + modelData.a
                            position: Qt.vector3d(0, 88, 0)
                            geometry: ConeGeometry { topRadius: 0; bottomRadius: 7; length: 18 }
                            pickable: true
                            materials: DefaultMaterial { lighting: DefaultMaterial.NoLighting; diffuseColor: modelData.c }
                        }
                        Model {  // scale box past the head
                            objectName: "s:" + modelData.a
                            source: "#Cube"
                            position: Qt.vector3d(0, 112, 0)
                            scale: Qt.vector3d(0.09, 0.09, 0.09)
                            pickable: true
                            materials: DefaultMaterial { lighting: DefaultMaterial.NoLighting; diffuseColor: modelData.c }
                        }
                        Model {  // ring: turning about this axis
                            objectName: "r:" + modelData.a
                            geometry: TorusGeometry { radius: 62; tubeRadius: 1.8; rings: 64; segments: 8 }
                            pickable: true
                            materials: DefaultMaterial { lighting: DefaultMaterial.NoLighting; diffuseColor: modelData.c }
                        }
                    }
                }
                Model {  // the centre box: even scale
                    objectName: "s:all"
                    source: "#Cube"
                    scale: Qt.vector3d(0.11, 0.11, 0.11)
                    pickable: true
                    materials: DefaultMaterial { lighting: DefaultMaterial.NoLighting; diffuseColor: "#d8d8d8" }
                }
            }
    }

    // ---- the orientation cube (top right): turns with the view; click a face = look straight at that side --------
    View3D {
        id: cubeView
        objectName: "cubeView"
        visible: root.measured && !root.activeCam
        width: 84
        height: 84
        anchors.top: parent.top
        anchors.right: parent.right
        anchors.margins: 6
        renderMode: View3D.Inline  // (a texture of its own drew one view move behind)
        camera: cubeCamera
        environment: SceneEnvironment {
            backgroundMode: SceneEnvironment.Transparent
            antialiasingMode: SceneEnvironment.MSAA
        }
        PerspectiveCamera {
            id: cubeCamera
            fieldOfView: 30
            clipNear: 10
            clipFar: 1000
            Component.onCompleted: { root.cubeCam = cubeCamera; root.follow() }
        }
        Repeater3D {  // six labelled faces (#Rectangle: 100 across, facing +Z, turned onto each side)
            model: [{n: "FRONT", p: Qt.vector3d(0, 0, 50), r: Qt.vector3d(0, 0, 0)},
                    {n: "BACK", p: Qt.vector3d(0, 0, -50), r: Qt.vector3d(0, 180, 0)},
                    {n: "RIGHT", p: Qt.vector3d(50, 0, 0), r: Qt.vector3d(0, 90, 0)},
                    {n: "LEFT", p: Qt.vector3d(-50, 0, 0), r: Qt.vector3d(0, -90, 0)},
                    {n: "TOP", p: Qt.vector3d(0, 50, 0), r: Qt.vector3d(-90, 0, 0)},
                    {n: "BOTTOM", p: Qt.vector3d(0, -50, 0), r: Qt.vector3d(90, 0, 0)}]
            delegate: Model {
                required property var modelData
                objectName: modelData.n
                source: "#Rectangle"
                position: modelData.p
                eulerRotation: modelData.r
                pickable: true
                materials: DefaultMaterial {
                    lighting: DefaultMaterial.NoLighting
                    diffuseMap: Texture {
                        sourceItem: Rectangle {
                            width: 128
                            height: 128
                            color: "#454b55"
                            border.color: "#9aa3ad"
                            border.width: 5
                            Text {
                                anchors.centerIn: parent
                                text: modelData.n
                                color: "#ececec"
                                font.pixelSize: modelData.n.length > 5 ? 22 : 26
                                font.bold: true
                            }
                        }
                    }
                }
            }
        }
    }

    MouseArea {  // the orbit camera's controls; editing: selecting, the handle, and moving a file camera
        anchors.fill: parent
        enabled: !root.activeCam || root.editable
        acceptedButtons: Qt.LeftButton | Qt.MiddleButton | Qt.RightButton
        property real lastX: 0
        property real lastY: 0
        property real pressX: 0
        property real pressY: 0
        property int button: 0
        property bool moved: false
        property bool onCube: false  // this press went to the orientation cube
        property vector3d target
        onPressed: function(mouse) {
            lastX = pressX = mouse.x; lastY = pressY = mouse.y; button = mouse.button; moved = false; onCube = false
            var face = button === Qt.LeftButton ? root.cubeFaceAt(mouse.x, mouse.y) : ""
            if (face !== "") {
                root.lookFrom(face)
                onCube = true
                return
            }
            if (root.editable && button === Qt.LeftButton && root.gizmoView && root.gizmoView.visible) {
                var hit = root.gizmoView.pick(mouse.x, mouse.y)
                var h = hit.objectHit ? hit.objectHit.objectName : ""
                if (h.indexOf(":") === 1) {
                    root.startHandle(h, mouse.x, mouse.y)
                    return
                }
            }
            if (root.editable && root.activeCam) {
                root.camBegin()
                return
            }
            if (button === Qt.RightButton)
                target = root.aimAt(mouse.x, mouse.y)
        }
        onReleased: function(mouse) {
            if (onCube)
                return
            if (root.dragHandle !== "") {
                root.endHandle()
                return
            }
            if (root.camStart !== null) {
                if (moved) root.camEnd(); else root.camStart = null
            }
            if (root.editable && !moved && mouse.button === Qt.LeftButton)
                root.pickSelect(mouse.x, mouse.y, (mouse.modifiers & Qt.ShiftModifier) !== 0)
        }
        onPositionChanged: function(mouse) {
            if (onCube)
                return
            if (Math.abs(mouse.x - pressX) + Math.abs(mouse.y - pressY) > 3)
                moved = true
            if (root.dragHandle !== "") {
                root.moveHandle(mouse.x, mouse.y)
                return
            }
            if (root.camStart !== null) {  // the camera looked through, moved (editing)
                var tx = mouse.x - pressX, ty = mouse.y - pressY
                if (button === Qt.LeftButton) root.camOrbit(tx, ty)
                else if (button === Qt.MiddleButton) root.camPan(tx, ty)
                else if (button === Qt.RightButton) root.camDolly((tx - ty) * 0.006)
                return
            }
            var dx = mouse.x - lastX, dy = mouse.y - lastY
            lastX = mouse.x; lastY = mouse.y
            if (!moved)
                return
            if (button === Qt.LeftButton) {          // orbit (from a straight side view: back to perspective first)
                root.leaveOrtho()
                root.yaw -= dx * 0.4
                root.pitch = Math.max(-89, Math.min(89, root.pitch - dy * 0.4))
                root.place()
            } else if (button === Qt.MiddleButton) { // pan: the scene follows the mouse
                var k = 2 * root.dist * Math.tan(camera.fieldOfView * Math.PI / 360) / view.height
                root.pivot = root.pivot.plus(camera.right.times(-dx * k)).plus(camera.up.times(dy * k))
                root.place()
            } else if (button === Qt.RightButton) {  // dolly toward the point clicked: right / up = in
                root.dolly((dx - dy) * 0.006, target)
            }
        }
        onDoubleClicked: function(mouse) { if (mouse.button === Qt.LeftButton && !root.activeCam) root.frame() }
        onWheel: function(wheel) {
            if (root.activeCam) {  // editing: dolly the camera looked through
                if (root.camStart === null)
                    root.camBegin()
                root.camStart.acc = (root.camStart.acc || 0) + wheel.angleDelta.y / 120 * 0.15
                root.camDolly(root.camStart.acc)
                wheelCommit.restart()
                return
            }
            root.dolly(wheel.angleDelta.y / 120 * 0.15, root.aimAt(wheel.x, wheel.y))
        }
    }
}
