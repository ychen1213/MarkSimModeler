"""Closed material surfaces in physical micrometres; STL is auxiliary geometry.

Parametric polygons are ear-clipped, retaining concavity. Voxel surfaces expose
cell faces exactly (no interpolating material IDs, smoothing or dimension drift).
Disconnected voxel bodies are exported independently. A non-manifold digital
surface is rejected instead of presenting a broken CAD object as valid.
"""
from __future__ import annotations
from pathlib import Path
import struct
import numpy as np
from scipy import ndimage


def _cross(a,b):
    return a[0]*b[1]-a[1]*b[0]


def triangulate_polygon(points):
    p = np.asarray(points,float)
    if p.ndim != 2 or p.shape[1] != 2 or len(p)<3 or not np.isfinite(p).all():
        raise ValueError('Expected finite XY polygon')
    scale = max(float(np.ptp(p,axis=0).max()),1e-9)
    eps = 1e-12*scale*scale
    if np.linalg.norm(p[0]-p[-1]) < 1e-12*scale: p=p[:-1]
    # Collinear vertices do not define a geometric corner and cause zero-area ears.
    while len(p)>3:
        remove = next((i for i in range(len(p)) if abs(_cross(p[i]-p[i-1],p[(i+1)%len(p)]-p[i]))<=eps),None)
        if remove is None: break
        p=np.delete(p,remove,axis=0)
    centered=p-p[0]
    area = sum(_cross(centered[i],centered[(i+1)%len(p)]) for i in range(len(p)))/2
    if abs(area)<=eps: raise ValueError('Degenerate polygon has zero area')
    if area<0: p=p[::-1].copy()
    n=len(p)
    # Reject crossed contours, including an unsplit GDS keyhole bridge. The
    # existing geometric builder decomposes those into simple solids first.
    for i in range(n):
        for j in range(i+1,n):
            if j in (i,(i+1)%n) or i==(j+1)%n: continue
            a,b,c,d=p[i],p[(i+1)%n],p[j],p[(j+1)%n]
            v=[_cross(b-a,c-a),_cross(b-a,d-a),_cross(d-c,a-c),_cross(d-c,b-c)]
            if v[0]*v[1]<-eps*eps and v[2]*v[3]<-eps*eps:
                raise ValueError('Self-intersecting polygon; process Boolean decomposition is required')
    remaining=list(range(n)); triangles=[]
    while len(remaining)>3:
        found=False
        for k,b in enumerate(remaining):
            a,c=remaining[k-1],remaining[(k+1)%len(remaining)]
            if _cross(p[b]-p[a],p[c]-p[b])<=eps: continue
            inside=False
            for q in remaining:
                if q in (a,b,c):continue
                if min(_cross(p[b]-p[a],p[q]-p[a]),_cross(p[c]-p[b],p[q]-p[b]),
                       _cross(p[a]-p[c],p[q]-p[c]))>=-eps:
                    inside=True;break
            if inside:continue
            triangles.append([a,b,c]);remaining.pop(k);found=True;break
        if not found: raise ValueError('Polygon cannot be triangulated as a simple nondegenerate contour')
    triangles.append(remaining)
    return p,np.asarray(triangles,dtype=np.int64)


def prism_mesh(solid):
    xy,top=triangulate_polygon(solid['polygon_xy_um'])
    n=len(xy); lo,hi=solid['z_min_um'],solid['z_max_um']
    vertices=np.vstack([np.column_stack([xy,np.full(n,lo)]),np.column_stack([xy,np.full(n,hi)])])
    triangles=[*top[:,::-1].tolist(),*(top+n).tolist()]
    for i in range(n):
        j=(i+1)%n
        triangles.extend([[i,j,j+n],[i,j+n,i+n]])
    result={**{k:solid[k] for k in ('name','material','source_step_id') if k in solid},
            'vertices_um':vertices,'triangles':np.asarray(triangles,dtype=np.int64)}
    validate_mesh(result)
    return result


def validate_mesh(mesh):
    v=np.asarray(mesh['vertices_um'],float); f=np.asarray(mesh['triangles'])
    if v.ndim!=2 or v.shape[1]!=3 or not np.isfinite(v).all() or f.ndim!=2 or f.shape[1]!=3 or not len(f):
        raise ValueError('Mesh requires finite Nx3 vertices and nonempty Mx3 triangles')
    if not np.issubdtype(f.dtype,np.integer) or f.min()<0 or f.max()>=len(v):
        raise ValueError('Invalid triangle vertex indices')
    xyz=v[f]; normals=np.cross(xyz[:,1]-xyz[:,0],xyz[:,2]-xyz[:,0])
    scale=max(float(np.ptp(v,axis=0).max()),1e-12)
    if np.any(np.linalg.norm(normals,axis=1)<=1e-14*scale*scale):
        raise ValueError('Mesh contains degenerate triangles')
    edges=np.concatenate([f[:,[0,1]],f[:,[1,2]],f[:,[2,0]]])
    undirected=np.sort(edges,axis=1)
    _,inverse,counts=np.unique(undirected,axis=0,return_inverse=True,return_counts=True)
    if np.any(counts!=2):
        raise ValueError('Mesh is open/non-manifold; every edge must belong to exactly two faces')
    orientation=np.bincount(inverse,weights=np.where(edges[:,0]<edges[:,1],1.,-1.))
    if np.any(orientation):raise ValueError('Mesh face orientations are inconsistent')
    # Two faces per edge is necessary but not sufficient: two closed sheets
    # can pinch at a single vertex. Each vertex's link must be ONE cycle.
    vertices=f.ravel(); face_ids=np.repeat(np.arange(len(f)),3)
    order=np.argsort(vertices,kind='stable')
    sorted_vertices=vertices[order]
    starts=np.r_[0,np.flatnonzero(np.diff(sorted_vertices))+1,len(order)]
    for start,end in zip(starts[:-1],starts[1:]):
        vertex=sorted_vertices[start];neighbors={}
        for face in f[face_ids[order[start:end]]]:
            a,b=map(int,face[face!=vertex])
            neighbors.setdefault(a,set()).add(b);neighbors.setdefault(b,set()).add(a)
        if any(len(adjacent)!=2 for adjacent in neighbors.values()):
            raise ValueError('Mesh has a non-manifold vertex link')
        reached=set();pending=[next(iter(neighbors))]
        while pending:
            current=pending.pop()
            if current in reached:continue
            reached.add(current);pending.extend(neighbors[current]-reached)
        if len(reached)!=len(neighbors):
            raise ValueError('Mesh has a non-manifold pinched vertex; disconnected surface links')
    # Shift before the signed-volume sum to avoid precision loss far from origin.
    centered=xyz-v.mean(axis=0)
    volume=float(np.einsum('ij,ij->i',centered[:,0],np.cross(centered[:,1],centered[:,2])).sum()/6)
    if volume<=0:raise ValueError('Mesh has nonpositive volume or inward faces')
    return {'closed':True,'oriented':True,'triangle_count':len(f),'volume_um3':volume,
            'bounds_um':np.column_stack([v.min(axis=0),v.max(axis=0)]).tolist()}


# (axis, sign, outward CCW integer-cell corner offsets).
_SIDES=((0,-1,((0,0,0),(0,0,1),(0,1,1),(0,1,0))),
        (0,1,((1,0,0),(1,1,0),(1,1,1),(1,0,1))),
        (1,-1,((0,0,0),(1,0,0),(1,0,1),(0,0,1))),
        (1,1,((0,1,0),(0,1,1),(1,1,1),(1,1,0))),
        (2,-1,((0,0,0),(0,1,0),(1,1,0),(1,0,0))),
        (2,1,((0,0,1),(1,0,1),(1,1,1),(0,1,1))))


def _voxel_component(mask,origin,spacing,name,material,max_triangles):
    faces=[];count=0
    for axis,sign,offsets in _SIDES:
        neighbor=np.zeros_like(mask)
        target=[slice(None)]*3; source=[slice(None)]*3
        target[axis]=slice(0,-1) if sign>0 else slice(1,None)
        source[axis]=slice(1,None) if sign>0 else slice(0,-1)
        neighbor[tuple(target)]=mask[tuple(source)]
        exposed=np.argwhere(mask & ~neighbor)
        count+=2*len(exposed)
        if count>max_triangles:raise ValueError('Voxel surface exceeds max_triangles; explicitly choose a coarser reconstruction grid')
        if len(exposed):faces.append(exposed[:,None,:]+np.asarray(offsets)[None,:,:])
    quads=np.concatenate(faces)
    vertices,inverse=np.unique(quads.reshape(-1,3),axis=0,return_inverse=True)
    q=inverse.reshape(-1,4)
    triangles=np.vstack([q[:,[0,1,2]],q[:,[0,2,3]]])
    mesh=dict(name=name,material=material,vertices_um=origin+vertices*spacing,triangles=triangles)
    check=validate_mesh(mesh)
    expected=float(mask.sum()*np.prod(spacing))
    if not np.isclose(check['volume_um3'],expected,rtol=1e-9,atol=1e-12):
        raise ValueError('Voxel surface volume differs from label volume')
    return mesh


def solid_meshes(model,max_triangles=2_000_000):
    from .exchange import validate_model
    validate_model(model)
    if type(max_triangles) is not int or max_triangles<=0:raise ValueError('max_triangles must be positive integer')
    g=model['geometry']; meshes=[]; remaining=max_triangles
    if g['representation']=='parametric_solids':
        for solid in g['solids']:
            if solid['material']==g['background_material']:continue
            mesh=prism_mesh(solid);remaining-=len(mesh['triangles'])
            if remaining<0:raise ValueError('Surface exceeds max_triangles')
            meshes.append(mesh)
    else:
        v=model['voxels']; ids=v['material_id']; origin=np.asarray(v['origin_um']); spacing=np.asarray(v['spacing_um'])
        for label in np.unique(ids):
            if label==v['background_id']:continue
            components,n=ndimage.label(ids==label,ndimage.generate_binary_structure(3,1))
            for part,box in enumerate(ndimage.find_objects(components),1):
                if box is None:continue
                start=np.array([s.start for s in box])
                mesh=_voxel_component(components[box]==part,origin+start*spacing,spacing,
                    f'{v["material_map"][str(label)]}_{part}',v['material_map'][str(label)],remaining)
                remaining-=len(mesh['triangles']);meshes.append(mesh)
    return meshes


def write_binary_stl(mesh,path):
    report=validate_mesh(mesh); path=Path(path)
    if path.exists():raise FileExistsError(f'Existing STL preserved: {path}')
    # STL float32 coordinates need an explicit precision check, especially for
    # small structures translated far from the origin. Never silently collapse.
    rounded={**mesh,'vertices_um':np.asarray(mesh['vertices_um'],dtype=np.float32).astype(float)}
    rounded_report=validate_mesh(rounded)
    original=np.asarray(mesh['vertices_um'],float)
    faces=np.asarray(mesh['triangles'])
    edges=np.concatenate([faces[:,[0,1]],faces[:,[1,2]],faces[:,[2,0]]])
    shortest_edge=float(np.linalg.norm(original[edges[:,0]]-original[edges[:,1]],axis=1).min())
    max_error=float(np.linalg.norm(rounded['vertices_um']-original,axis=1).max())
    volume_error=abs(rounded_report['volume_um3']/report['volume_um3']-1)
    # At most 0.1% of the smallest surface edge and 0.1% volume error. Topology
    # alone cannot detect a 10 nm feature rounded to 7.8 nm far from the origin.
    if max_error>shortest_edge*1e-3 or volume_error>1e-3:
        raise ValueError('STL float32 precision would change feature dimensions; use native LSF without STL or explicitly recenter the source geometry')
    triangles=rounded['vertices_um'][mesh['triangles']]
    normals=np.cross(triangles[:,1]-triangles[:,0],triangles[:,2]-triangles[:,0])
    normals/=np.linalg.norm(normals,axis=1)[:,None]
    dtype=np.dtype([('normal','<f4',(3,)),('vertices','<f4',(3,3)),('attribute','<u2')])
    records=np.zeros(len(triangles),dtype=dtype);records['normal']=normals;records['vertices']=triangles
    path.parent.mkdir(parents=True,exist_ok=True)
    with path.open('xb') as f:
        f.write(b'MarkSimModeler physical coordinates in micrometres; material in manifest'.ljust(80,b' ')[:80])
        f.write(struct.pack('<I',len(triangles)));f.write(records.tobytes())
    return {**rounded_report,'source_volume_um3':report['volume_um3'],
            'max_vertex_rounding_um':max_error,'relative_volume_rounding':volume_error,
            'file':str(path),'coordinate_unit':'um','lumerical_scaling_factor':1e-6}
