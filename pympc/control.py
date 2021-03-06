import time
import sys, os
import numpy as np
import scipy.linalg as linalg
import matplotlib.pyplot as plt
import gurobipy as grb
from contextlib import contextmanager
from optimization.pnnls import linear_program
from optimization.gurobi import quadratic_program, real_variable
from geometry.polytope import Polytope
from dynamical_systems import DTAffineSystem, DTPWASystem
from optimization.mpqpsolver import MPQPSolver, CriticalRegion
# from scipy.spatial import ConvexHull
from pympc.geometry.convex_hull import ConvexHull
import cdd
from pympc.geometry.convex_hull import PolytopeProjectionInnerApproximation
from copy import copy




class MPCController:
    """
    Model Predictive Controller.

    VARIABLES:
        sys: DTLinearSystem (see dynamical_systems.py)
        N: horizon of the controller
        objective_norm: 'one' or 'two'
        Q, R, P: weight matrices for the controller (state stage cost, input stage cost, and state terminal cost, respectively)
        X, U: state and input domains (see the Polytope class in geometry.polytope)
        X_N: terminal constraint (Polytope)
    """

    def __init__(self, sys, N, objective_norm, Q, R, P=None, X=None, U=None, X_N=None):
        self.sys = sys
        self.N = N
        self.objective_norm = objective_norm
        self.Q = Q
        self.R = R
        if P is None:
            self.P = Q
        else:
            self.P = P
        self.X = X
        self.U = U
        if X_N is None and X is not None:
            self.X_N = X
        else:
            self.X_N = X_N
        self.condense_program()
        self.critical_regions = None
        return

    def condense_program(self):
        """
        Depending on the norm of the controller, creates a parametric LP or QP in the initial state of the system (see ParametricLP or ParametricQP classes).
        """
        c = np.zeros((self.sys.n_x, 1))
        a_sys = DTAffineSystem(self.sys.A, self.sys.B, c)
        sys_list = [a_sys]*self.N
        X_list = [self.X]*self.N
        U_list = [self.U]*self.N
        switching_sequence = [0]*self.N
        pwa_sys = DTPWASystem.from_orthogonal_domains(sys_list, X_list, U_list)
        self.condensed_program = OCP_condenser(pwa_sys, self.objective_norm, self.Q, self.R, self.P, self.X_N, switching_sequence)
        self.remove_intial_state_contraints()
        return

    def remove_intial_state_contraints(self, tol=1e-10):
        """
        This is needed since OCP_condenser() is the same for PWA systems anb linear systems. OCP for PWA systems have constraints also on the initial state of the system (x(0) has to belong to a domain of the state partition of the PWA system). For linear system this constraint has to be removed.
        """
        C_u_rows_norm = list(np.linalg.norm(self.condensed_program.C_u, axis=1))
        intial_state_contraints = [i for i, row_norm in enumerate(C_u_rows_norm) if row_norm < tol]
        if len(intial_state_contraints) > self.X.lhs_min.shape[0]:
            raise ValueError('Wrong number of zero rows in the constrinats')
        self.condensed_program.C_u = np.delete(self.condensed_program.C_u,intial_state_contraints, 0)
        self.condensed_program.C_x = np.delete(self.condensed_program.C_x,intial_state_contraints, 0)
        self.condensed_program.C = np.delete(self.condensed_program.C,intial_state_contraints, 0)
        return

    def feedforward(self, x0):
        """
        Given the state of the system, returns the optimal sequence of N inputs and the related cost.
        """
        u_feedforward, cost = self.condensed_program.solve(x0)
        u_feedforward = [u_feedforward[self.sys.n_u*i:self.sys.n_u*(i+1),:] for i in range(self.N)]
        # if any(np.isnan(u_feedforward).flatten()):
        #     print('Unfeasible initial condition x_0 = ' + str(x0.tolist()))
        return u_feedforward, cost

    def feedback(self, x0):
        """
        Returns the a single input vector (the first of feedforward(x0)).
        """
        return self.feedforward(x0)[0][0]

    def get_explicit_solution(self):
        """
        (Only for controllers with norm = 2).
        Returns the partition of the state space in critical regions (explicit MPC solution).
        Temporary fix: since the method remove_intial_state_contraints() modifies the variables of condensed_program, I have to call remove_linear_terms() again!
        """
        self.condensed_program.remove_linear_terms()
        mpqp_solution = MPQPSolver(self.condensed_program)
        self.critical_regions = mpqp_solution.critical_regions
        return

    def feedforward_explicit(self, x0):
        """
        Finds the critical region where the state x0 is, and returns the PWA feedforward.
        """
        if self.critical_regions is None:
            raise ValueError('Explicit solution not available, call .get_explicit_solution()')
        cr_x0 = self.critical_regions.lookup(x0)
        if cr_x0 is not None:
            u_feedforward = cr_x0.u_offset + cr_x0.u_linear.dot(x0)
            u_feedforward = [u_feedforward[self.sys.n_u*i:self.sys.n_u*(i+1),:] for i in range(self.N)]
            cost = .5*x0.T.dot(cr_x0.V_quadratic).dot(x0) + cr_x0.V_linear.dot(x0) + cr_x0.V_offset
            cost = cost[0,0]
        else:
            # print('Unfeasible initial condition x_0 = ' + str(x0.tolist()))
            u_feedforward = [np.full((self.sys.n_u, 1), np.nan) for i in range(self.N)]
            cost = np.nan
        return u_feedforward, cost

    def feedback_explicit(self, x0):
        """
        Returns the a single input vector (the first of feedforward_explicit(x0)).
        """
        return self.feedforward(x0)[0][0]

    def optimal_value_function(self, x0):
        """
        Returns the optimal value function for the state x0.
        """
        if self.critical_regions is None:
            raise ValueError('Explicit solution not available, call .get_explicit_solution()')
        cr_x0 = self.critical_regions.lookup(x0)
        if cr_x0 is not None:
            cost = .5*x0.T.dot(cr_x0.V_quadratic).dot(x0) + cr_x0.V_linear.dot(x0) + cr_x0.V_offset
        else:
            #print('Unfeasible initial condition x_0 = ' + str(x0.tolist()))
            cost = np.nan
        return cost

    # def group_critical_regions(self):
    #     self.u_offset_list = []
    #     self.u_linear_list = []
    #     self.cr_families = []
    #     for cr in self.critical_regions:
    #         cr_family = np.where(np.isclose(cr.u_offset[0], self.u_offset_list))[0]
    #         if cr_family and all(np.isclose(cr.u_linear[0,:], self.u_linear_list[cr_family[0]])):
    #             self.cr_families[cr_family[0]].append(cr)
    #         else:
    #             self.cr_families.append([cr])
    #             self.u_offset_list.append(cr.u_offset[0])
    #             self.u_linear_list.append(cr.u_linear[0,:])
    #     print 'Critical regions grouped in ', str(len(self.cr_families)), ' sets.'
    #     return



class MPCHybridController:
    """
    Hybrid Model Predictive Controller.

    VARIABLES:
        sys: DTPWASystem (see dynamical_systems.py)
        N: horizon of the controller
        objective_norm: 'one' or 'two'
        Q, R, P: weight matrices for the controller (state stage cost, input stage cost, and state terminal cost, respectively)
        X_N: terminal constraint (Polytope)
    """

    def __init__(self, sys, N, objective_norm, Q, R, P, X_N):
        self.sys = sys
        self.N = N
        self.objective_norm = objective_norm
        self.Q = Q
        self.R = R
        self.P = P
        self.X_N = X_N
        self._get_bigM_domains()
        self._get_bigM_dynamics()
        self._MIP_model()
        return

    def _get_bigM_domains(self, tol=1.e-8):
        """
        Computes all the bigMs for the domains of the PWA system.
        When the system is in mode j, n_i bigMs are needed to drop each one the n_i inequalities of the polytopic domain for the mode i.
        With s number of modes, M_domains is a list containing s lists, each list then constains s bigM vector of size n_i.
        M_domains[j][i] is used to drop the inequalities of the domain i when the system is in mode j.
        """
        self._M_domains = []
        for i, domain_i in enumerate(self.sys.domains):
            M_i = []
            for j, domain_j in enumerate(self.sys.domains):
                M_ij = []
                if i != j:
                    for k in range(domain_i.lhs_min.shape[0]):
                        sol = linear_program(-domain_i.lhs_min[k,:], domain_j.lhs_min, domain_j.rhs_min)
                        M_ijk = (- sol.min - domain_i.rhs_min[k])[0]
                        M_ij.append(M_ijk)
                M_ij = np.reshape(M_ij, (len(M_ij), 1))
                M_i.append(M_ij)
            self._M_domains.append(M_i)
        return

    def _get_bigM_dynamics(self, tol=1.e-8):
        """
        Computes all the smallMs and bigMs for the dynamics of the PWA system.
        When the system is in mode j, n_x smallMs and n_x bigMs are needed to drop the equations of motion for the mode i.
        With s number of modes, m_dynamics and M_dynamics are two lists containing s lists, each list then constains s bigM vector of size n_x.
        m_dynamics[j][i] and M_dynamics[j][i] are used to drop the equations of motion of the mode i when the system is in mode j.
        """
        self._M_dynamics = []
        self._m_dynamics = []
        for i in range(self.sys.n_sys):
            M_i = []
            m_i = []
            lhs_i = np.hstack((self.sys.affine_systems[i].A, self.sys.affine_systems[i].B))
            rhs_i = self.sys.affine_systems[i].c
            for domain_j in self.sys.domains:
                M_ij = []
                m_ij = []
                for k in range(lhs_i.shape[0]):
                    sol = linear_program(-lhs_i[k,:], domain_j.lhs_min, domain_j.rhs_min)
                    M_ijk = (- sol.min + rhs_i[k])[0]
                    M_ij.append(M_ijk)
                    sol = linear_program(lhs_i[k,:], domain_j.lhs_min, domain_j.rhs_min)
                    m_ijk = (sol.min + rhs_i[k])[0]
                    m_ij.append(m_ijk)
                M_ij = np.reshape(M_ij, (len(M_ij), 1))
                m_ij = np.reshape(m_ij, (len(m_ij), 1))
                M_i.append(M_ij)
                m_i.append(np.array(m_ij))
            self._M_dynamics.append(M_i)
            self._m_dynamics.append(m_i)
        return

    def _MIP_model(self):
        self._model = grb.Model()
        self._MIP_variables()
        self._MIP_objective()
        self._MIP_constraints()
        self._MIP_parameters()
        self._model.update()
        return

    def _MIP_variables(self):
        x = self._add_real_variable([self.N+1, self.sys.n_x], name='x')
        u = self._add_real_variable([self.N, self.sys.n_u], name='u')
        self._x_np = [np.array([[x[k,i]] for i in range(self.sys.n_x)]) for k in range(self.N+1)]
        self._u_np = [np.array([[u[k,i]] for i in range(self.sys.n_u)]) for k in range(self.N)]
        self._z = self._add_real_variable([self.N, self.sys.n_sys, self.sys.n_x], name='z')
        self._d = self._model.addVars(self.N, self.sys.n_sys, vtype=grb.GRB.BINARY, name='d')
        self._model.update()
        return

    def _add_real_variable(self, dimensions, name, **kwargs):
        lb_var = [-grb.GRB.INFINITY]
        for d in dimensions:
            lb_var = [lb_var * d]
        var = self._model.addVars(*dimensions, lb=lb_var, name=name, **kwargs)
        return var

    def _MIP_objective(self):
        if self.objective_norm == 'one':
            self._MILP_objective()
        elif self.objective_norm == 'two':
            self._MIQP_objective()
        else:
            raise ValueError('Unknown norm ' + self.objective_norm + '.')
        return

    def _MILP_objective(self):
        Q = clean_matrix(self.Q)
        R = clean_matrix(self.R)
        P = clean_matrix(self.P)
        self._phi = self._model.addVars(self.N+1, self.sys.n_x, name='phi')
        self._psi = self._model.addVars(self.N, self.sys.n_u, name='psi')
        self._model.update()
        V = 0.
        for k in range(self.N+1):
            for i in range(self.sys.n_x):
                V += self._phi[k,i]
        for k in range(self.N):
            for i in range(self.sys.n_u):
                V += self._psi[k,i]
        self._model.setObjective(V)
        for k in range(self.N):
            for i in range(self.sys.n_x):
                self._model.addConstr(self._phi[k,i] >= Q[i,:].dot(self._x_np[k])[0])
                self._model.addConstr(self._phi[k,i] >= - Q[i,:].dot(self._x_np[k])[0])
            for i in range(self.sys.n_u):
                self._model.addConstr(self._psi[k,i] >= R[i,:].dot(self._u_np[k])[0])
                self._model.addConstr(self._psi[k,i] >= - R[i,:].dot(self._u_np[k])[0])
        for i in range(self.sys.n_x):
            self._model.addConstr(self._phi[self.N,i] >= P[i,:].dot(self._x_np[self.N])[0])
            self._model.addConstr(self._phi[self.N,i] >= - P[i,:].dot(self._x_np[self.N])[0])
        return

    def _MIQP_objective(self):
        Q = clean_matrix(self.Q)
        R = clean_matrix(self.R)
        P = clean_matrix(self.P)
        V = 0.
        for k in range(self.N):
            V += self._x_np[k].T.dot(Q).dot(self._x_np[k]) + self._u_np[k].T.dot(R).dot(self._u_np[k])
        V += self._x_np[self.N].T.dot(P).dot(self._x_np[self.N])
        self._model.setObjective(V[0,0])
        return

    def _MIP_constraints(self):
        self._disjunction_modes()
        self._constraint_domains()
        self._constraint_dynamics()
        self._terminal_contraint()
        return

    def _disjunction_modes(self):
        for k in range(self.N):
            #self._model.addConstr(np.sum([self._d[k,i] for i in range(self.sys.n_sys)]) == 1.)
            self._model.addSOS(grb.GRB.SOS_TYPE1, [self._d[k,i] for i in range(self.sys.n_sys)], [1]*self.sys.n_sys)
        return

    def _constraint_domains(self):
        M_domains = [[clean_matrix(self._M_domains[i][j]) for j in range(self.sys.n_sys)] for i in range(self.sys.n_sys)]
        for i in range(self.sys.n_sys):
            lhs = clean_matrix(self.sys.domains[i].lhs_min)
            rhs = clean_matrix(self.sys.domains[i].rhs_min)
            for k in range(self.N):
                expr_xu = lhs.dot(np.vstack((self._x_np[k], self._u_np[k]))) - rhs
                expr_M = np.sum([M_domains[i][j]*self._d[k,j] for j in range(self.sys.n_sys) if j != i], axis=0)
                expr = expr_xu - expr_M
                self._model.addConstrs((expr[j][0] <= 0. for j in range(len(expr))))
        return

    def _constraint_dynamics(self):
        M_dynamics = [[clean_matrix(self._M_dynamics[i][j]) for j in range(self.sys.n_sys)] for i in range(self.sys.n_sys)]
        m_dynamics = [[clean_matrix(self._m_dynamics[i][j]) for j in range(self.sys.n_sys)] for i in range(self.sys.n_sys)]
        for k in range(self.N):
            for j in range(self.sys.n_x):
                expr = 0.
                for i in range(self.sys.n_sys):
                    expr += self._z[k,i,j]
                self._model.addConstr(self._x_np[k+1][j,0] == expr)
        for k in range(self.N):
            for i in range(self.sys.n_sys):
                expr_M = M_dynamics[i][i]*self._d[k,i]
                expr_m = m_dynamics[i][i]*self._d[k,i]
                for j in range(self.sys.n_x):
                    self._model.addConstr(self._z[k,i,j] <= expr_M[j,0])
                    self._model.addConstr(self._z[k,i,j] >= expr_m[j,0])
        for i in range(self.sys.n_sys):
            A = clean_matrix(self.sys.affine_systems[i].A)
            B = clean_matrix(self.sys.affine_systems[i].B)
            c = clean_matrix(self.sys.affine_systems[i].c)
            for k in range(self.N):
                expr = A.dot(self._x_np[k]) + B.dot(self._u_np[k]) + c
                expr_M = expr - np.sum([M_dynamics[i][j]*self._d[k,j] for j in range(self.sys.n_sys) if j != i], axis=0)
                expr_m = expr - np.sum([m_dynamics[i][j]*self._d[k,j] for j in range(self.sys.n_sys) if j != i], axis=0)
                for j in range(self.sys.n_x):
                    self._model.addConstr(self._z[k,i,j] >= expr_M[j,0])
                    self._model.addConstr(self._z[k,i,j] <= expr_m[j,0])
        return

    def _terminal_contraint(self):
        lhs = clean_matrix(self.X_N.lhs_min) 
        rhs = clean_matrix(self.X_N.rhs_min) 
        expr = lhs.dot(self._x_np[self.N]) - rhs
        self._model.addConstrs((expr[i,0] <= 0. for i in range(len(self.X_N.minimal_facets))))
        return

    def _MIP_parameters(self):
        self._model.setParam('OutputFlag', False)
        time_limit = 600.
        self._model.setParam('TimeLimit', time_limit)
        self._model.setParam(grb.GRB.Param.OptimalityTol, 1.e-9)
        self._model.setParam(grb.GRB.Param.FeasibilityTol, 1.e-9)
        self._model.setParam(grb.GRB.Param.IntFeasTol, 1.e-9)
        self._model.setParam(grb.GRB.Param.MIPGap, 1.e-9)
        return

    def feedforward(self, x0, u_ws=None, x_ws=None, ss_ws=None):

        # initial condition
        for i in range(self.sys.n_x):
            if self._model.getConstrByName('intial_condition_' + str(i)) is not None:
                self._model.remove(self._model.getConstrByName('intial_condition_' + str(i)))
            self._model.addConstr(self._x_np[0][i,0] == x0[i,0], name='intial_condition_' + str(i))

        # warm start
        if u_ws is not None:
            self._warm_start_input(u_ws)
        if x_ws is not None:
            self._warm_start_state(x_ws)
        if ss_ws is not None:
            self._warm_start_modes(ss_ws)
        if x_ws is not None and ss_ws is not None:
            self._warm_start_sum_of_states(x_ws, ss_ws)
        if self.objective_norm == 'one' and (x_ws is not None or u_ws is not None):
            self._warm_start_slack_variables_MILP(x_ws, u_ws)
        self._model.update()

        # run optimization
        self._model.optimize()

        return self._return_solution()

    def _warm_start_input(self, u_ws):
        for i in range(self.sys.n_u):
            for k in range(self.N):
                self._u_np[k][i,0].setAttr('Start', u_ws[k][i,0])
        return

    def _warm_start_state(self, x_ws):
        for i in range(self.sys.n_x):
            for k in range(self.N+1):
                self._x_np[k][i,0].setAttr('Start', x_ws[k][i,0])
        return

    def _warm_start_modes(self, ss_ws):
        for j in range(self.sys.n_sys):
            for k in range(self.N):
                if j == ss_ws[k]:
                    self._d[k,j].setAttr('Start', 1.)
                else:
                    self._d[k,j].setAttr('Start', 0.)
        return

    def _warm_start_sum_of_states(self, x_ws, ss_ws):
        for i in range(self.sys.n_x):
            for j in range(self.sys.n_sys):
                for k in range(self.N):
                    if j == ss_ws[k]:
                        self._z[k,j,i].setAttr('Start', x_ws[k+1][i,0])
                    else:
                        self._z[k,j,i].setAttr('Start', 0.)
        return

    def _warm_start_slack_variables_MILP(self, x_ws, u_ws):
        if x_ws is not None:
            for i in range(self.sys.n_x):
                for k in range(self.N):
                    self._phi[k,i].setAttr('Start', self.Q[i,:].dot(x_ws[k]))
                self._phi[self.N,i].setAttr('Start', self.P[i,:].dot(x_ws[self.N]))
        if u_ws is not None:
            for i in range(self.sys.n_u):
                for k in range(self.N):
                    self._psi[k,i].setAttr('Start', self.R[i,:].dot(u_ws[k]))
        return

    def _return_solution(self):
        if self._model.status == grb.GRB.Status.OPTIMAL:
            cost = self._model.objVal
            u_feedforward = [np.array([[self._u_np[k][i,0].x] for i in range(self.sys.n_u)]) for k in range(self.N)]
            x_trajectory = [np.array([[self._x_np[k][i,0].x] for i in range(self.sys.n_x)]) for k in range(self.N+1)]
            d_star = [np.array([[self._d[k,i].x] for i in range(self.sys.n_sys)]) for k in range(self.N)]
            switching_sequence = [np.where(np.isclose(d, 1.))[0][0] for d in d_star]
        else:
            if self._model.status == grb.GRB.Status.TIME_LIMIT:
                print('The solution of the MIQP excedeed the time limit of ' + str(time_limit) + '.')
            u_feedforward = [np.full((self.sys.n_u,1), np.nan) for k in range(self.N)]
            x_trajectory = [np.full((self.sys.n_x,1), np.nan) for k in range(self.N+1)]
            cost = np.nan
            switching_sequence = [np.nan]*self.N
        return u_feedforward, x_trajectory, tuple(switching_sequence), cost


    def feedback(self, x0):
        """
        Reuturns the first input from feedforward().
        """
        return self.feedforward(x0)[0][0]

    def condense_program(self, switching_sequence):
        """
        Given a mode sequence, returns the condensed LP or QP (see ParametricLP or ParametricQP classes).
        """
        # tic = time.time()
        # print('\nCondensing the OCP for the switching sequence ' + str(switching_sequence) + ' ...')
        if len(switching_sequence) != self.N:
            raise ValueError('Switching sequence not coherent with the controller horizon.')
        prog = OCP_condenser(self.sys, self.objective_norm, self.Q, self.R, self.P, self.X_N, switching_sequence)
        # print('... OCP condensed in ' + str(time.time() -tic ) + ' seconds.\n')
        return prog



class FeasibleSetLibrary:
    """
    library[switching_sequence]
    - program
    - feasible_set
    """

    def __init__(self, controller):
        self.controller = controller
        self.library = dict()
        return

    def sample_policy(self, n_samples, check_sample_function=None):
        n_rejected = 0
        n_included = 0
        n_new_ss = 0
        n_unfeasible = 0
        for i in range(n_samples):
            try:
                print('Sample ' + str(i) + ': ')
                x = self.random_sample(check_sample_function)
                if not self.sampling_rejection(x):
                    print('solving MIQP... '),
                    tic = time.time()
                    ss = self.controller.feedforward(x)[2]
                    print('solution found in ' + str(time.time()-tic) + ' s.')
                    if not any(np.isnan(ss)):
                        if self.library.has_key(ss):
                            n_included += 1
                            print('included.')
                            print('including sample in inner approximation... '),
                            tic = time.time()
                            self.library[ss]['feasible_set'].include_point(x)
                            print('sample included in ' + str(time.time()-tic) + ' s.')
                        else:
                            n_new_ss += 1
                            print('new switching sequence ' + str(ss) + '.')
                            self.library[ss] = dict()
                            print('condensing QP... '),
                            tic = time.time()
                            prog = self.controller.condense_program(ss)
                            print('QP condensed in ' + str(time.time()-tic) + ' s.')
                            self.library[ss]['program'] = prog
                            lhs = np.hstack((-prog.C_x, prog.C_u))
                            rhs = prog.C
                            residual_dimensions = range(prog.C_x.shape[1])
                            print('constructing inner simplex... '),
                            tic = time.time()
                            feasible_set = PolytopeProjectionInnerApproximation(lhs, rhs, residual_dimensions)
                            print('inner simplex constructed in ' + str(time.time()-tic) + ' s.')
                            print('including sample in inner approximation... '),
                            tic = time.time()
                            feasible_set.include_point(x)
                            print('sample included in ' + str(time.time()-tic) + ' s.')
                            self.library[ss]['feasible_set'] = feasible_set
                    else:
                        n_unfeasible += 1
                        print('unfeasible.')
                else:
                    n_rejected += 1
                    print('rejected.')
            except ValueError:
                print 'Something went wrong with this sample...'
                pass
        print('\nTotal number of samples: ' + str(n_samples) + ', switching sequences found: ' + str(n_new_ss) + ', included samples: ' + str(n_included) + ', rejected samples: ' + str(n_rejected) + ', unfeasible samples: ' + str(n_unfeasible) + '.')
        return

    def random_sample(self, check_sample_function=None):
        if check_sample_function is None:
            x = np.random.rand(self.controller.sys.n_x, 1)
            x = np.multiply(x, (self.controller.sys.x_max - self.controller.sys.x_min)) + self.controller.sys.x_min
        else:
            is_inside = False
            while not is_inside:
                x = np.random.rand(self.controller.sys.n_x,1)
                x = np.multiply(x, (self.controller.sys.x_max - self.controller.sys.x_min)) + self.controller.sys.x_min
                is_inside = check_sample_function(x)
        return x

    def sampling_rejection(self, x):
        for ss_value in self.library.values():
            if ss_value['feasible_set'].applies_to(x):
                return True
        return False

    def get_feasible_switching_sequences(self, x):
        return [ss for ss, ss_values in self.library.items() if ss_values['feasible_set'].applies_to(x)]

    def feedforward(self, x, given_ss=None, max_qp= None):
        V_list = []
        V_star = np.nan
        u_star = [np.full((self.controller.sys.n_u, 1), np.nan) for i in range(self.controller.N)]
        ss_star = [np.nan]*self.controller.N
        fss = self.get_feasible_switching_sequences(x)
        print 'number of feasible QPs:', len(fss)
        if given_ss is not None:
            fss.insert(0, given_ss)
        if not fss:
            return u_star, V_star, ss_star, V_list
        else:
            if max_qp is not None and max_qp < len(fss):
                fss = fss[:max_qp]
                print 'number of QPs limited to', max_qp
            for ss in fss:
                u, V = self.library[ss]['program'].solve(x)
                V_list.append(V)
                if V < V_star or (np.isnan(V_star) and not np.isnan(V)):
                    V_star = V
                    u_star = [u[i*self.controller.sys.n_u:(i+1)*self.controller.sys.n_u,:] for i in range(self.controller.N)]
                    ss_star = ss
        return u_star, V_star, ss_star, V_list

    def feedback(self, x, given_ss=None, max_qp= None):
        u_star, V_star, ss_star = self.feedforward(x, given_ss, max_qp)[0:-1]
        return u_star[0], ss_star

    def add_shifted_switching_sequences(self, terminal_domain):
        for ss in self.library.keys():
            for shifted_ss in self.shift_switching_sequence(ss, terminal_domain):
                if not self.library.has_key(shifted_ss):
                    self.library[shifted_ss] = dict()
                    self.library[shifted_ss]['program'] = self.controller.condense_program(shifted_ss)
                    self.library[shifted_ss]['feasible_set'] = EmptyFeasibleSet()

    @staticmethod
    def shift_switching_sequence(ss, terminal_domain):
        return [ss[i:] + (terminal_domain,)*i for i in range(1,len(ss))]

    def plot_partition(self):
        for ss_value in self.library.values():
            color = np.random.rand(3,1)
            fs = ss_value['feasible_set']
            if not fs.empty:
                p = Polytope(fs.hull.A, fs.hull.b)
                p.assemble()#redundant=False, vertices=fs.hull.points)
                p.plot(facecolor=color, alpha=.5)
        return



class EmptyFeasibleSet:

    def __init__(self):
        self.empty = True
        return

    def applies_to(self, x):
        return False


class ParametricLP:

    def __init__(self, F_u, F_x, F, C_u, C_x, C):
        """
        LP in the form:
        min  \sum_i | (F_u u + F_x x + F)_i |
        s.t. C_u u <= C_x x + C
        """
        self.F_u = F_u
        self.F_x = F_x
        self.F = F
        self.C_u = C_u
        self.C_x = C_x
        self.C = C
        self.add_slack_variables()
        return

    def add_slack_variables(self):
        """
        Reformulates the LP as:
        min f^T z
        s.t. A z <= B x + c
        """
        n_slack = self.F.shape[0]
        n_u = self.F_u.shape[1]
        self.f = np.vstack((
            np.zeros((n_u,1)),
            np.ones((n_slack,1))
            ))
        self.A = np.vstack((
            np.hstack((self.C_u, np.zeros((self.C_u.shape[0], n_slack)))),
            np.hstack((self.F_u, -np.eye(n_slack))),
            np.hstack((-self.F_u, -np.eye(n_slack)))
            ))
        self.B = np.vstack((self.C_x, -self.F_x, self.F_x))
        self.c = np.vstack((self.C, -self.F, self.F))
        self.n_var = n_u + n_slack
        self.n_cons = self.A.shape[0]
        return

    def solve(self, x0, u_length=None):
        x0 = np.reshape(x0, (x0.shape[0], 1))
        sol = linear_program(self.f, self.A, self.B.dot(x0)+self.c)
        u_star = sol.argmin[0:self.F_u.shape[1]]
        if u_length is not None:
            if not float(u_star.shape[0]/u_length).is_integer():
                raise ValueError('Uncoherent dimension of the input u_length.')
            u_star = [u_star[i*u_length:(i+1)*u_length,:] for i in range(u_star.shape[0]/u_length)]
        return u_star, sol.min


class ParametricQP:

    def __init__(self, F_uu, F_xu, F_xx, F_u, F_x, F, C_u, C_x, C):
        """
        Multiparametric QP in the form:
        min  .5 u' F_{uu} u + x0' F_{xu} u + F_u' u + .5 x0' F_{xx} x0 + F_x' x0 + F
        s.t. C_u u <= C_x x + C
        """
        self.F_uu = F_uu
        self.F_xx = F_xx
        self.F_xu = F_xu
        self.F_u = F_u
        self.F_x = F_x
        self.F = F
        self.C_u = C_u
        self.C_x = C_x
        self.C = C
        self.remove_linear_terms()
        self._feasible_set = None
        return

    def solve(self, x0, u_length=None):
        x0 = np.reshape(x0, (x0.shape[0], 1))
        H = self.F_uu
        f = x0.T.dot(self.F_xu) + self.F_u.T
        A = self.C_u
        b = self.C + self.C_x.dot(x0)
        u_star, cost = quadratic_program(H, f, A, b)
        cost += .5*x0.T.dot(self.F_xx).dot(x0) + self.F_x.T.dot(x0) + self.F
        if u_length is not None:
            if not float(u_star.shape[0]/u_length).is_integer():
                raise ValueError('Uncoherent dimension of the input u_length.')
            u_star = [u_star[i*u_length:(i+1)*u_length,:] for i in range(u_star.shape[0]/u_length)]
        return u_star, cost[0,0]

    def get_active_set(self, x, u, tol=1.e-6):
        u = np.vstack(u)
        return tuple(np.where((self.C_u.dot(u) - self.C - self.C_x.dot(x)) > -tol)[0])

    def remove_linear_terms(self):
        """
        Applies the change of variables z = u + F_uu^-1 (F_xu' x + F_u')
        that puts the cost function in the form
        V = 1/2 z' H z + 1/2 x' F_xx_q x + F_x_q' x + F_q
        and the constraints in the form:
        G u <= W + S x
        """
        self.H_inv = np.linalg.inv(self.F_uu)
        self.H = self.F_uu
        self.F_xx_q = self.F_xx - self.F_xu.dot(self.H_inv).dot(self.F_xu.T)
        self.F_x_q = self.F_x - self.F_xu.dot(self.H_inv).dot(self.F_u)
        self.F_q = self.F - .5*self.F_u.T.dot(self.H_inv).dot(self.F_u)
        self.G = self.C_u
        self.S = self.C_x + self.C_u.dot(self.H_inv).dot(self.F_xu.T)
        self.W = self.C + self.C_u.dot(self.H_inv).dot(self.F_u)
        return

    @property
    def feasible_set(self):
        if self._feasible_set is None:
            augmented_polytope = Polytope(np.hstack((- self.C_x, self.C_u)), self.C)
            augmented_polytope.assemble()
            if augmented_polytope.empty:
                return None
            self._feasible_set = augmented_polytope.orthogonal_projection(range(self.C_x.shape[1]))
        return self._feasible_set


    def get_z_sensitivity(self, active_set):
        # clean active set
        G_A = self.G[active_set,:]
        if active_set and np.linalg.matrix_rank(G_A) < G_A.shape[0]:
            lir = linearly_independent_rows(G_A)
            active_set = [active_set[i] for i in lir]

        # multipliers explicit solution
        inactive_set = sorted(list(set(range(self.C.shape[0])) - set(active_set)))
        [G_A, W_A, S_A] = [self.G[active_set,:], self.W[active_set,:], self.S[active_set,:]]
        [G_I, W_I, S_I] = [self.G[inactive_set,:], self.W[inactive_set,:], self.S[inactive_set,:]]
        H_A = np.linalg.inv(G_A.dot(self.H_inv).dot(G_A.T))
        lambda_A_offset = - H_A.dot(W_A)
        lambda_A_linear = - H_A.dot(S_A)

        # primal variables explicit solution
        z_offset = - self.H_inv.dot(G_A.T.dot(lambda_A_offset))
        z_linear = - self.H_inv.dot(G_A.T.dot(lambda_A_linear))
        return z_offset, z_linear

    def get_u_sensitivity(self, active_set):
        z_offset, z_linear = self.get_z_sensitivity(active_set)

        # primal original variables explicit solution
        u_offset = z_offset - self.H_inv.dot(self.F_u)
        u_linear = z_linear - self.H_inv.dot(self.F_xu.T)
        return u_offset, u_linear

    def get_cost_sensitivity(self, x_list, active_set):
        z_offset, z_linear = self.get_z_sensitivity(active_set)

        # optimal value function explicit solution: V_star = .5 x' V_quadratic x + V_linear x + V_offset
        V_quadratic = z_linear.T.dot(self.H).dot(z_linear) + self.F_xx_q
        V_linear = z_offset.T.dot(self.H).dot(z_linear) + self.F_x_q.T
        V_offset = self.F_q + .5*z_offset.T.dot(self.H).dot(z_offset)

        # tangent approximation
        plane_list = []
        for x in x_list:
            A = x.T.dot(V_quadratic) + V_linear
            b = -.5*x.T.dot(V_quadratic).dot(x) + V_offset
            plane_list.append([A, b])

        return plane_list

    def solve_free_x(self):
        H = np.vstack((
            np.hstack((self.F_uu, self.F_xu.T)),
            np.hstack((self.F_xu, self.F_xx))
            ))
        f = np.vstack((self.F_u, self.F_x))
        A = np.hstack((self.C_u, -self.C_x))
        b = self.C
        z_star, cost = quadratic_program(H, f, A, b)
        u_star = z_star[0:self.F_uu.shape[0],:]
        x_star = z_star[self.F_uu.shape[0]:,:]
        return u_star, x_star, cost

### AUXILIARY FUNCTIONS

@contextmanager
def suppress_stdout():
    with open(os.devnull, "w") as devnull:
        old_stdout = sys.stdout
        sys.stdout = devnull
        try:
            yield
        finally:
            sys.stdout = old_stdout

def linearly_independent_rows(A, tol=1.e-6):
    R = linalg.qr(A.T)[1]
    R_diag = np.abs(np.diag(R))
    return list(np.where(R_diag > tol)[0])

def clean_matrix(M, tol=1.e-7):
    M_clean = copy(M)
    for i in range(M.shape[0]):
        for j in range(M.shape[1]):
            if np.abs(M[i,j]) < tol:
                M_clean[i,j] = 0.
    return M_clean

def OCP_condenser(sys, objective_norm, Q, R, P, X_N, switching_sequence):
    tic = time.time()
    N = len(switching_sequence)
    Q_bar = linalg.block_diag(*[Q for i in range(N)] + [P])
    R_bar = linalg.block_diag(*[R for i in range(N)])
    G, W, E = constraint_condenser(sys, X_N, switching_sequence)
    if objective_norm == 'one':
        F_u, F_x, F = linear_objective_condenser(sys, Q_bar, R_bar, switching_sequence)
        parametric_program = ParametricLP(F_u, F_x, F, G, E, W)
    elif objective_norm == 'two':
        F_uu, F_xu, F_xx, F_u, F_x, F = quadratic_objective_condenser(sys, Q_bar, R_bar, switching_sequence)
        parametric_program = ParametricQP(F_uu, F_xu, F_xx, F_u, F_x, F, G, E, W)
    # print 'total condensing time is', str(time.time()-tic),'s.\n'
    return parametric_program

def constraint_condenser(sys, X_N, switching_sequence):
    N = len(switching_sequence)
    D_sequence = [sys.domains[switching_sequence[i]] for i in range(N)]
    lhs_x = linalg.block_diag(*[D.lhs_min[:,:sys.n_x] for D in D_sequence] + [X_N.lhs_min])
    lhs_u = linalg.block_diag(*[D.lhs_min[:,sys.n_x:] for D in D_sequence])
    lhs_u = np.vstack((lhs_u, np.zeros((X_N.lhs_min.shape[0], lhs_u.shape[1]))))
    rhs = np.vstack([D.rhs_min for D in D_sequence] + [X_N.rhs_min])
    A_bar, B_bar, c_bar = sys.condense(switching_sequence)
    G = (lhs_x.dot(B_bar) + lhs_u)
    W = rhs - lhs_x.dot(c_bar)
    E = - lhs_x.dot(A_bar)
    # # the following might be super slow (and is not necessary)
    # n_ineq_before = G.shape[0]
    # tic = time.time()
    # p = Polytope(np.hstack((G, -E)), W)
    # p.assemble()
    # if not p.empty:
    #     G = p.lhs_min[:,:sys.n_u*N]
    #     E = - p.lhs_min[:,sys.n_u*N:]
    #     W = p.rhs_min
    #     n_ineq_after = G.shape[0]
    # else:
    #     G = None
    #     W = None
    #     E = None
    # print '\n' + str(n_ineq_before - n_ineq_after) + 'on' + str(n_ineq_before) + 'redundant inequalities removed in', str(time.time()-tic),'s,',
    return G, W, E

def linear_objective_condenser(sys, Q_bar, R_bar, switching_sequence):
    """
    \sum_i | (F_u u + F_x x + F)_i |
    """
    A_bar, B_bar, c_bar = sys.condense(switching_sequence)
    F_u = np.vstack((Q_bar.dot(B_bar), R_bar))
    F_x = np.vstack((Q_bar.dot(A_bar), np.zeros((R_bar.shape[0], A_bar.shape[1]))))
    F = np.vstack((Q_bar.dot(c_bar), np.zeros((R_bar.shape[0], 1))))
    return F_u, F_x, F

def quadratic_objective_condenser(sys, Q_bar, R_bar, switching_sequence):
    """
    .5 u' F_{uu} u + x0' F_{xu} u + F_u' u + .5 x0' F_{xx} x0 + F_x' x0 + F
    """
    A_bar, B_bar, c_bar = sys.condense(switching_sequence)
    F_uu = 2*(R_bar + B_bar.T.dot(Q_bar).dot(B_bar))
    F_xu = 2*A_bar.T.dot(Q_bar).dot(B_bar)
    F_xx = 2.*A_bar.T.dot(Q_bar).dot(A_bar)
    F_u = 2.*B_bar.T.dot(Q_bar).dot(c_bar)
    F_x = 2.*A_bar.T.dot(Q_bar).dot(c_bar)
    F = c_bar.T.dot(Q_bar).dot(c_bar)
    return F_uu, F_xu, F_xx, F_u, F_x, F

def remove_initial_state_constraints(prog, tol=1e-10):
    C_u_rows_norm = list(np.linalg.norm(prog.C_u, axis=1))
    intial_state_contraints = [i for i, row_norm in enumerate(C_u_rows_norm) if row_norm < tol]
    prog.C_u = np.delete(prog.C_u,intial_state_contraints, 0)
    prog.C_x = np.delete(prog.C_x,intial_state_contraints, 0)
    prog.C = np.delete(prog.C,intial_state_contraints, 0)
    prog.remove_linear_terms()
    return prog


def explict_solution_from_hybrid_condensing(prog, tol=1e-10):
    porg = remove_initial_state_constraints(prog)
    mpqp_solution = MPQPSolver(prog)
    critical_regions = mpqp_solution.critical_regions
    return critical_regions