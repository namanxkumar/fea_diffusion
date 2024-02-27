from sfepy.discrete.fem import Mesh, FEDomain, Field
from sfepy.discrete import (
    FieldVariable,
    Material,
    Integral,
    Function,
    Equation,
    Equations,
    Problem,
    Variables,
)
from sfepy.discrete.common.region import Region
from sfepy.mechanics.matcoefs import stiffness_from_youngpoisson
from sfepy.terms import Term, Terms
from sfepy.discrete.conditions import Conditions, EssentialBC
from sfepy.base.base import IndexedStruct
from sfepy.solvers.ls import ScipyDirect
from sfepy.solvers.nls import Newton
from sfepy.solvers.ts_solvers import SimpleTimeSteppingSolver
from sfepy.base.base import Struct, output

from .custom_plotter import plot

import numpy as np
from typing import Tuple, List, Dict
import os
from os import path
import math
from PIL import Image


class FEAnalysis:
    def __init__(
        self,
        filename: str,
        data_dir: str,
        condition_dir: str,
        #material_division: Dict[Tuple, List],
        force_vertex_tags_magnitudes: List[Tuple[int, Tuple[float, float]]],
        force_edges_tags_magnitudes: List[Tuple[Tuple[int, int], Tuple[int, int]]],
        constraints_vertex_tags: List[int],
        constraints_edges_tags: List[Tuple[int, int]],
         
        youngs_modulus: float = 210000,
        poisson_ratio: float = 0.3,
        num_steps: int = 11,
        save_meshes=False,
    ):
        self.data_dir = data_dir
        self.region_filename = "regions"
        self.save_meshes = save_meshes
        self.condition_dir = condition_dir
        # self.solution_filename = "solution.vtk"
        self.initial_image_size = math.ceil(512 / 0.685546875)
        self.image_size = self.initial_image_size
        self.bounds = (0, 0, self.initial_image_size, self.initial_image_size)
        self.common_config = "-2 --color-map binary --no-scalar-bars --no-axes --window-size {},{} --off-screen".format(
            self.initial_image_size, self.initial_image_size
        )

        self.mesh = Mesh.from_file(path.join(data_dir, filename))
        
        self.domain = FEDomain("domain", self.mesh) 
        self.omega = self.domain.create_region("Omega", "all")

        field = Field.from_args("fu", np.float64, "vector", self.omega, approx_order=1)

        self.unknown_field = FieldVariable("u", "unknown", field)
        self.test_field = FieldVariable("v", "test", field, primary_var_name="u")

        self.integral_0 = Integral("i0", order=0)
        # self.1_integral = Integral('i', order=1)
        self.integral_2 = Integral("i2", order=2)

        self.material = self._create_material("m", youngs_modulus, poisson_ratio)

        self.force_region_name_list = []
        self.constraint_region_name_list = []

        # Create force terms
        force_vertex_region_magnitudes = []
        for index in range(len(force_vertex_tags_magnitudes)):
            region_name = "VertexForce{}".format(index)
            region = self._create_region_from_vertex(
                region_name, force_vertex_tags_magnitudes[index][0]
            )
            self.force_region_name_list.append(region_name)
            magnitude = self._create_magnitude(
                "VertexMagnitude{}".format(index),
                force_vertex_tags_magnitudes[index][1],
            )
            force_vertex_region_magnitudes.append((region, magnitude))
            with open(
                path.join(condition_dir, "magnitudes.txt".format(index)), "a+"
            ) as f:
                f.write(
                    "{}:{}\n".format(
                        region_name, str(force_vertex_tags_magnitudes[index][1])
                    )
                )

        force_edges_region_magnitudes = []
        for index in range(len(force_edges_tags_magnitudes)):
            region_name = "EdgeForce{}".format(index)
            region = self._create_region_from_edge(
                region_name, force_edges_tags_magnitudes[index][0]
            )
            self.force_region_name_list.append(region_name)
            num_vertices = max(len(region.get_entities(0)), 1)
            magnitude = self._create_magnitude(
                "EdgeMagnitude{}".format(index),
                tuple(
                    component / num_vertices
                    for component in force_edges_tags_magnitudes[index][1]
                ),
            )
            force_edges_region_magnitudes.append((region, magnitude))
            with open(
                path.join(condition_dir, "magnitudes.txt".format(index)), "a+"
            ) as f:
                f.write(
                    "{}:{}\n".format(
                        region_name,
                        str(
                            tuple(
                                component / num_vertices
                                for component in force_edges_tags_magnitudes[index][1]
                            )
                        ),
                    )
                )

        force_regions_magnitudes = (
            force_vertex_region_magnitudes + force_edges_region_magnitudes
        )

        self.force_terms = [
            self._create_load_term(region, magnitude)
            for region, magnitude in force_regions_magnitudes
        ]

        # Create fixed constraints
        constraints_vertex_regions = self._create_regions_from_vertices(
            "VertexConstraint", constraints_vertex_tags
        )
        constraints_edge_regions = self._create_regions_from_edges(
            "EdgeConstraint", constraints_edges_tags
        )

        constraints_regions = constraints_vertex_regions + constraints_edge_regions

        self.fixed_constraints = self._create_fixed_constraints(
            "Fixed", constraints_regions
        )
        self.region_material_list= self.create_regions_materials(material_division,self.domain)
        self.lhs_terms_list= self.create_lhs_terms(self.region_material_list,self.integral_2,self.test_field,self.unknown_field)

        self.lhs_term = Term.new(
            "dw_lin_elastic(m.D, v, u)",
            self.integral_2,
            self.omega,
            m=self.material,
            v=self.test_field,
            u=self.unknown_field,
        )

        self.nls_solver = self._create_nls_solver()
        self.num_steps = num_steps

    @staticmethod
    def crop_image(image_path, bounds):
        image = Image.open(image_path)
        image = image.crop(bounds)
        image.save(image_path)

    @staticmethod
    def _get_points_on_edge(coords, bounding_tags, **kwargs):
        coord0 = coords[bounding_tags[0] - 1]
        coord1 = coords[bounding_tags[1] - 1]
        x1, y1 = coord1[0] - coord0[0], coord1[1] - coord0[1]
        x2, y2 = coords[:, 0] - coord0[0], coords[:, 1] - coord0[1]
        return np.where(np.abs(x1 * y2 - x2 * y1) < 1e-14)[0]

    def _create_region_from_vertex(self, name: str, vertex_tag: int) -> Region:
        return self.domain.create_region(
            name, "vertex {}".format(vertex_tag - 1), "vertex"
        )

    def _create_region_from_edge(self, name: str, edge_tags: Tuple[int, int]) -> Region:
        return self.domain.create_region(
            name,
            "vertices by get_edge",
            "facet",
            functions={
                "get_edge": Function(
                    "get_edge",
                    self._get_points_on_edge,
                    extra_args={"bounding_tags": edge_tags},
                )
            },
            allow_empty=True,
        )

    def _create_regions_from_vertices(
        self, name: str, vertices_tag: List[int]
    ) -> List[Region]:
        regions = []
        for index in range(len(vertices_tag)):
            region_name = name + str(index)
            regions.append(
                self._create_region_from_vertex(region_name, vertices_tag[index])
            )
            self.constraint_region_name_list.append(region_name)
        return regions

    def _create_regions_from_edges(
        self, name: str, edges_tags: List[Tuple[int, int]]
    ) -> List[Region]:
        regions = []
        for index in range(len(edges_tags)):
            region_name = name + str(index)
            regions.append(
                self._create_region_from_edge(region_name, edges_tags[index])
            )
            self.constraint_region_name_list.append(region_name)
        return regions

    def _material_region_from_vertices(self,vertices: List, domain, name: str) -> Region:
        obj = Region(name, 'given vertices', domain, '')
        obj.vertices = vertices
        return obj

    @staticmethod
    def _create_material(
        name: str, youngs_modulus: int, poisson_ratio: int
    ) -> Material:
        return Material(
            name,
            D=stiffness_from_youngpoisson(
                dim=2, young=youngs_modulus, poisson=poisson_ratio
            ),
        )
    
    
    def _create_regions_materials(self,material_vertices_dict, domain) -> List[Region]:
        region_material_list = []  # list of tuples of (region_obj, material_obj)
        for key, vertices in material_vertices_dict.items():
            y_modulus, p_ratio = key
            current_region = self._material_region_from_vertices(vertices, domain, f"Region_{y_modulus}_{p_ratio}")  
            current_material = self._create_material(f"Material_{y_modulus}_{p_ratio}", y_modulus, p_ratio)
            region_material_list.append((current_region, current_material))

        return region_material_list

    def _create_lhs_terms(self,region_material_list, integral_2, test_field, unknown_field) -> List[Term]:
        lhs_terms_list = []
        for region, material in region_material_list:
            lhs_term = Term.new(
                "dw_lin_elastic(m.D, v, u)",
                integral_2,
                region,
                m=material,
                v=test_field,
                u=unknown_field,
            )
            lhs_terms_list.append(lhs_term)

        return lhs_terms_list


    @staticmethod
    def _timestep_magnitude(ts, coords, mode, magnitude: Tuple[float, float], **kwargs):
        if mode == "special":
            force = (ts.time * -1 * magnitude[0], ts.time * -1 * magnitude[1])
            # val = np.array([
            #     [force[0]],
            #     [force[1]]
            #     ])
            # if ltype == 'point':
            return {"val": list(force)}
            # elif ltype == 'edge':
            #     return {'val' : val}

    def _create_magnitude(self, name: str, magnitude: Tuple[int, int]) -> Material:
        return Material(
            name,
            function=Function(
                "timestep_magnitude",
                self._timestep_magnitude,
                extra_args={"magnitude": magnitude},
            ),
        )

    def _create_load_term(self, region: Region, magnitude: Material) -> Term:
        # if region.kind == 'vertex':
        return Term.new(
            "dw_point_load(f.val, v)",
            self.integral_0,
            region,
            f=magnitude,
            v=self.test_field,
        )
        # elif region.kind == 'facet':
        #     return Term.new('dw_surface_ltr(f.val, v)', self.integral_2, region, f=magnitude, v=self.test_field)

    @staticmethod
    def _create_equations(
        name: str, lhs_terms: List[Term], rhs_terms: List[Term]
    ) -> Equations:
        
        rhs_terms = Terms(rhs_terms)
        equations = Equations()
        for i in range(len(lhs_terms)):
            equations.append(Equation(name, lhs_terms[i]+ rhs_terms))
        return equations

    @staticmethod
    def _create_fixed_constraints(name: str, regions: List[Region]) -> Conditions:
        conditions = []
        for index in range(len(regions)):
            conditions.append(
                EssentialBC(name + str(index), regions[index], {"u.all": 0.0})
            )
        return Conditions(conditions)

    @staticmethod
    def _create_nls_solver() -> Newton:
        ls = ScipyDirect({})
        nls_status = IndexedStruct()
        return Newton({}, lin_solver=ls, status=nls_status)
 
    def _save_regions(self, problem: Problem) -> None:
        directory = self.condition_dir if self.save_meshes else self.data_dir
        if path.isfile(path.join(directory, self.region_filename + ".vtk")):
            os.remove(path.join(directory, self.region_filename + ".vtk"))
        problem.save_regions_as_groups(path.join(directory, self.region_filename))

        # if self.save_meshes:
        #     # save copy of regions file in condition directory
        #     if path.isfile(
        #         path.join(self.condition_dir, self.region_filename + ".vtk")
        #     ):
        #         os.remove(path.join(self.condition_dir, self.region_filename + ".vtk"))
        #     os.rename(
        #         path.join(self.data_dir, self.region_filename + ".vtk"),
        #         path.join(self.condition_dir, self.region_filename + ".vtk"),
        #     )

    # def _save_solution(self, problem: Problem, output) -> None:
    #     problem.save_state(self.solution_filename, out=output)

    @staticmethod
    def calculate_stress_strain(
        output: dict, problem: Problem, *args, **kwargs
    ) -> dict:
        strain = problem.evaluate("ev_cauchy_strain.2.Omega(u)", mode="el_avg")
        stress = problem.evaluate(
            "ev_cauchy_stress.2.Omega(m.D, u)", mode="el_avg", copy_materials=False
        )
        # print(problem.equations.variables['u'].data)
        # print(np.array(strain[1].create_output()['cauchy_strain'].data), np.array(stress).shape)
        output["cauchy_strain"] = Struct(
            name="output_data", mode="cell", data=strain, dofs=None
        )
        output["cauchy_stress"] = Struct(
            name="output_data", mode="cell", data=stress, dofs=None
        )
        # print(strain_field.coors, stress_field.coors)
        # print(np.max(np.squeeze(np.array(output['cauchy_strain'].data)), axis=0), np.max(np.squeeze(np.array(output['cauchy_stress'].data)), axis=0))

        return output

    def calculate(self):
        equations = self._create_equations("Balance", self.lhs_term, self.force_terms)

        problem = Problem("Elasticity", equations=equations)
        problem.set_bcs(ebcs=self.fixed_constraints)
        problem.set_ics(ics=Conditions([]))

        ts_solver = SimpleTimeSteppingSolver(
            {"n_step": self.num_steps},
            nls=self.nls_solver,
            context=problem,
            verbose=True,
        )

        problem.set_solver(ts_solver)

        self._save_regions(problem)

        problem.output_dir = self.condition_dir if self.save_meshes else self.data_dir
        variables: Variables = problem.solve(
            save_results=True, post_process_hook=self.calculate_stress_strain
        )

        # if self.save_meshes:
        #     # copy solution files to condition directory domain.xx.vtk where xx is 00 to 11
        #     for step in range(self.num_steps):
        #         if path.isfile(
        #             path.join(self.condition_dir, "domain.{:0>2}.vtk".format(step))
        #         ):
        #             os.remove(
        #                 path.join(self.condition_dir, "domain.{:0>2}.vtk".format(step))
        #             )
        #         os.rename(
        #             path.join(self.data_dir, "domain.{:0>2}.vtk".format(step)),
        #             path.join(self.condition_dir, "domain.{:0>2}.vtk".format(step)),
        #         )
        # print(np.array(variables.create_output()['u'].data).shape)
        # print(variables.get_state_parts())

    def update_image_size_or_bounds(self, image_size=None, bounds=None):
        if image_size is not None:
            self.image_size = image_size
            self.common_config = "-2 --color-map binary --no-scalar-bars --no-axes --window-size {},{} --off-screen".format(
                image_size, image_size
            )
        if bounds is not None:
            self.bounds = bounds

    def save_input_image(self, filepath, input_filepath=None, outline=False, crop=True):
        if self.num_steps <= 10:
            domainfilename = "domain.0.vtk"
        else:
            domainfilename = "domain.00.vtk"
        if input_filepath is None:
            if self.save_meshes:
                input_filepath = path.join(self.condition_dir, domainfilename)
            else:
                input_filepath = path.join(self.data_dir, domainfilename)
        if outline:
            plot(
                filenames=[input_filepath],
                fields=[("1", "vs")],
                window_size=(self.image_size, self.image_size),
                outline=True,
                screenshot=filepath,
            )
            # system("sfepy-view {} -s 0 -f 1:vs {} --outline -o {}".format(input_filepath, self.common_config, filepath))
        else:
            plot(
                filenames=[input_filepath],
                fields=[("1", "vs")],
                window_size=(self.image_size, self.image_size),
                screenshot=filepath,
            )
            # system("sfepy-view {} -s 0 -f 1:vs {} -o {}".format(input_filepath, self.common_config, filepath))

        if crop:
            self.crop_image(filepath, self.bounds)

    def save_region_images(self, filepathroot, crop=True):
        for config in self.force_region_name_list + self.constraint_region_name_list:
            filepath = "{}_{}.png".format(filepathroot, config)

            # system("sfepy-view {}.vtk -f {}:vs {} -o {}".format(path.join(self.data_dir, self.region_filename), config, self.common_config, filepath))
            if self.save_meshes:
                directory = self.condition_dir
            else:
                directory = self.data_dir
            plot(
                filenames=["{}.vtk".format(path.join(directory, self.region_filename))],
                fields=[(config, "vs")],
                window_size=(self.image_size, self.image_size),
                screenshot=filepath,
            )

            if crop:
                self.crop_image(filepath, self.bounds)

    def save_output_images(
        self,
        filepathroot,
        save_displacement=True,
        save_stress=True,
        save_strain=True,
        crop=True,
    ):
        displacement_config = {
            "displacement_x": [("u", "c0")],
            "displacement_y": [("u", "c1")],
        }

        stress_config = {
            "stress_x": [("cauchy_stress", "c0")],
            "stress_y": [("cauchy_stress", "c1")],
        }

        strain_config = {
            "strain_x": [("cauchy_strain", "c0")],
            "strain_y": [("cauchy_strain", "c1")],
        }

        output_file_config = {}

        if save_displacement:
            output_file_config.update(displacement_config)

        if save_stress:
            output_file_config.update(stress_config)

        if save_strain:
            output_file_config.update(strain_config)

        for step in range(self.num_steps):
            for type, config in output_file_config.items():
                filepath = "{}_{}_{}.png".format(filepathroot, type, step)
                # system("sfepy-view domain.??.vtk -f {} -s {} {} -o {}".format(config, step, self.common_config, filename))

                # these values are found by trial and error and correspond to the current force magnitude range (max 5000N), need updating if force magnitude range changes
                if type == "displacement_x" or type == "displacement_y":
                    scalar_bar_range = [-0.05, 0.05]
                elif type == "stress_x" or type == "stress_y":
                    scalar_bar_range = [
                        -5e5,
                        5e5,
                    ]  # along x axis: [-17e5, 6.55e5], along y axis: [-3.66e5, 7.86e5]
                elif type == "strain_x" or type == "strain_y":
                    scalar_bar_range = [-10, 10]

                if self.save_meshes:
                    directory = self.condition_dir
                else:
                    directory = self.data_dir

                if self.num_steps <= 10:
                    domainfilename = "domain.{}.vtk"
                else:
                    domainfilename = "domain.{:0>2}.vtk"

                # plot(filenames=["domain.{:0>2}.vtk".format(s) for s in range(self.num_steps)], fields=config, step=step, window_size=(self.image_size, self.image_size), screenshot=filepath, show_scalar_bars=True)
                plot(
                    filenames=[
                        path.join(directory, domainfilename.format(s))
                        for s in range(self.num_steps)
                    ],
                    fields=config,
                    step=step,
                    window_size=(self.image_size, self.image_size),
                    screenshot=filepath,
                    scalar_bar_range=scalar_bar_range,
                )

                if crop:
                    # pass
                    self.crop_image(filepath, self.bounds)
